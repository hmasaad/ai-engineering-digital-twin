"""Propagate dependency risk through the modules that import it.

The index starts at a package: a recorded build failure, a recent upgrade,
a native plugin, or an import the manifest does not declare. Direct importers
take that index. Modules that import those files take a smaller share, out to
three hops. A trace then follows a vulnerability, a failed or changed
dependency, or a structural weak point through the same importers, with a
severity, a likelihood index, the path, and a consequence. It is a
decision-support index and does not move the bug, incident, delay, or
performance scores.
"""

from __future__ import annotations

from collections import deque

from app.graph import adjacency, find_cycles, reachable
from app.temporal import holds

FAILURE = 0.62
UPGRADE = 0.24
PLUGIN = 0.16
UNDECLARED = 0.40
UPGRADE_WINDOW_DAYS = 90.0
MAX_DEPTH = 3
MIN_RISK = 0.05


def propagate(view: dict) -> dict:
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    declared = set((view.get("project") or {}).get("dependencies") or [])
    modules = [module for module in view.get("modules") or [] if module.get("role") == "code"]
    by_path = {module["path"]: module for module in modules}
    importers = _direct_importers(modules)
    signals = _package_signals(facts, declared, set(importers))

    packages = []
    module_parts: dict[str, list[tuple[str, float, int]]] = {module["path"]: [] for module in modules}
    reverse = adjacency(view.get("edges") or [], reverse=True)
    for name, parts in sorted(signals.items()):
        risk = _combine([item["weight"] for item in parts])
        if risk < MIN_RISK:
            continue
        directs = importers.get(name) or []
        depths = reachable(directs, reverse, MAX_DEPTH) if directs else {}
        for path, depth in depths.items():
            if path not in by_path:
                continue
            module_parts[path].append((name, risk * _exposure(depth), depth))
        version, manifest = _pin(facts, name)
        packages.append(
            {
                "name": name,
                "version": version,
                "manifest": manifest,
                "risk": risk,
                "drivers": [item["label"] for item in parts],
                "signals": parts,
                "direct": len(directs),
                "reaches": len(depths),
            }
        )
    packages.sort(key=lambda item: (-item["risk"], item["name"]))

    rows = []
    for path, shares in module_parts.items():
        if not shares:
            continue
        risk = _combine([share for _name, share, _depth in shares])
        if risk < MIN_RISK:
            continue
        lead = max(shares, key=lambda item: (item[1], -item[2], item[0]))
        rows.append(
            {
                "module": path,
                "service": by_path[path]["service"],
                "risk": risk,
                "depth": lead[2],
                "via": lead[0],
                "packages": sorted({name for name, _share, _depth in shares}),
            }
        )
    rows.sort(key=lambda item: (-item["risk"], item["depth"], item["module"]))
    traces = _traces(view, by_path, signals)
    report = {
        "packages": packages[:8],
        "modules": rows[:12],
        "traces": traces,
        "summary": _summary(packages),
        "note": (
            "Dependency risk starts at the package and decays through importers. "
            "A direct import keeps the package index. Each further hop keeps a smaller share, out to depth 3. "
            "A trace starts at a vulnerable file, a failed or upgraded dependency, or a structural weak point, "
            "then follows importers. Each step has a severity, a likelihood index, the import path, and a consequence read from that path. "
            "This does not move the bug, incident, delay, or performance indexes, and the likelihood index is not a probability. "
            "Ten algorithms then follow direction, weights, a conditional index, a belief update, a fault tree, common causes, a replay, one containment cut, a band, and a calibration check. "
            "They multiply hops and count a shared package or cycle once. They do not add node indexes, and they do not rewrite the package index."
        ),
    }
    from app.propagate_algorithms import run_algorithms

    report["algorithms"] = run_algorithms(view, report)
    return report


def _package_signals(facts: list[dict], declared: set[str], imported: set[str]) -> dict[str, list[dict]]:
    signals: dict[str, list[dict]] = {}

    def add(name: str, key: str, weight: float, label: str, age: float | None = None) -> None:
        if not name:
            return
        bucket = signals.setdefault(name, [])
        if any(item["label"] == label for item in bucket):
            return
        bucket.append({"key": key, "weight": weight, "label": label, "age": age})

    failures = {
        fact.get("subject"): fact
        for fact in facts
        if fact.get("predicate") == "attributed_to" and fact.get("kind") == "event"
    }
    for fact in failures.values():
        age = fact.get("at_age")
        when = f"{_age(age)}d ago" if age is not None else "in history"
        add(_pkg_name(str(fact.get("object") or "")), "failure", FAILURE, f"Build failure {when}", None if age is None else float(age))

    for fact in facts:
        if fact.get("predicate") != "depends_on" or not holds(fact, 0):
            continue
        name = _pkg_name(str(fact.get("object") or ""))
        opened = float(fact.get("from_age") or 0)
        if 0 < opened <= UPGRADE_WINDOW_DAYS:
            add(name, "upgrade", UPGRADE, f"Upgraded {_age(opened)}d ago", opened)

    for fact in facts:
        if fact.get("predicate") != "plugin" or not holds(fact, 0):
            continue
        add(str(fact.get("subject") or ""), "plugin", PLUGIN, f"Binds {fact.get('object')}")

    for name in sorted(imported - declared):
        add(name, "undeclared", UNDECLARED, "Imported and missing from the manifest")
    return signals


def _direct_importers(modules: list[dict]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for module in modules:
        for raw in module.get("external_imports") or []:
            name = _pkg_name(str(raw))
            bucket = found.setdefault(name, [])
            if module["path"] not in bucket:
                bucket.append(module["path"])
    return found


def _pin(facts: list[dict], name: str) -> tuple[str, str]:
    for fact in facts:
        if fact.get("predicate") != "depends_on" or not holds(fact, 0):
            continue
        label = str(fact.get("object") or "")
        if _pkg_name(label) != name:
            continue
        version = label.split("@", 1)[1] if "@" in label and not label.startswith("@") else ""
        if label.startswith("@") and label.count("@") >= 2:
            version = label.rsplit("@", 1)[1]
        return version, str(fact.get("subject") or "")
    return "", ""


def _exposure(depth: int) -> float:
    if depth <= 0:
        return 1.0
    return 0.55 / depth


def _combine(parts: list[float]) -> float:
    survival = 1.0
    for part in parts:
        survival *= 1.0 - max(0.0, min(0.95, part))
    return round(1.0 - survival, 4)


def _summary(packages: list[dict]) -> str:
    if not packages:
        return "No dependency is carrying risk into the modules."
    lead = packages[0]
    direct = lead["direct"]
    indirect = lead["reaches"] - direct
    noun = "module" if direct == 1 else "modules"
    if direct == 0:
        reach = "It does not reach a module."
    elif indirect == 0:
        reach = f"It reaches {direct} {noun} directly."
    else:
        reach = f"It reaches {direct} {noun} directly and {indirect} more through imports."
    return f"{lead['name']} carries the highest dependency index at {lead['risk']:.2f}. {reach}"


def _pkg_name(label: str) -> str:
    if label.startswith("@") and label.count("@") >= 2:
        return label.rsplit("@", 1)[0]
    if "@" in label and not label.startswith("@"):
        return label.split("@", 1)[0]
    return label


VULNERABILITY = {
    "sql-injection": (0.72, "SQL injection in this file"),
    "dangerous-exec": (0.68, "Dynamic code execution in this file"),
    "hardcoded-secret": (0.62, "Hard-coded secret in this file"),
    "unverified-tls": (0.44, "TLS verification is disabled in this file"),
}
KIND_LABEL = {
    "vulnerability": "Vulnerability",
    "failure": "Failure",
    "dependency_change": "Dependency change",
    "architecture": "Architectural weakness",
}
KIND_RANK = {"vulnerability": 0, "failure": 1, "dependency_change": 2, "architecture": 3}
CAPABILITY = {
    "auth": "authentication",
    "login": "sign-in",
    "session": "session handling",
    "profile": "profile access",
    "token": "credential handling",
    "gateway": "the request entry",
    "main": "the process entry",
    "index": "an application entry",
    "checkout": "checkout",
    "charge": "charging",
    "retry": "payment retry",
    "stock": "inventory reservation",
    "pricing": "pricing",
    "email": "mail delivery",
    "db": "data access",
}


def _traces(view: dict, by_path: dict, signals: dict[str, list[dict]]) -> list[dict]:
    reverse = adjacency(view.get("edges") or [], reverse=True)
    cycles = {path for cycle in find_cycles(view.get("edges") or []) for path in cycle}
    origins = []
    for path, module in by_path.items():
        causes = _causes(module, signals, reverse, cycles, by_path)
        if not causes:
            continue
        score = _combine([cause["weight"] for cause in causes])
        if score < 0.2:
            continue
        primary = min(causes, key=lambda cause: (-cause["weight"], KIND_RANK[cause["kind"]]))
        origins.append((score, path, module, causes, primary))
    origins.sort(key=lambda item: (-item[0], item[1]))
    traces = []
    for score, path, module, causes, primary in origins[:6]:
        dist, parent = _walk([path], reverse)
        affected = []
        for reached, depth in sorted(dist.items(), key=lambda item: (item[1], item[0])):
            if reached not in by_path:
                continue
            chain = _chain(reached, parent)
            exposure = _exposure(depth)
            strength = round(score * exposure, 4)
            other = by_path[reached]
            likelihood = round(strength * (0.45 + 0.55 * float(other.get("test_gap") or 0)), 4)
            width = 0.08 if primary["kind"] in {"vulnerability", "failure"} else 0.14
            parent_path = chain[-2] if len(chain) >= 2 else ""
            affected.append(
                {
                    "module": reached,
                    "service": other["service"],
                    "depth": depth,
                    "severity": _severity(strength),
                    "severity_score": strength,
                    "likelihood": likelihood,
                    "low": round(max(0.0, likelihood - width), 4),
                    "high": round(min(1.0, likelihood + width), 4),
                    "path": chain,
                    "consequence": _consequence(reached, depth, parent_path, by_path, chain),
                }
            )
        kind = primary["kind"]
        label = KIND_LABEL[kind].lower()
        article = "An" if label[0] in "aeiou" else "A"
        noun = "module" if len(affected) == 1 else "modules"
        detail = ". ".join(cause["label"].rstrip(".") for cause in causes[:3])
        traces.append(
            {
                "origin": path,
                "service": module["service"],
                "kind": kind,
                "kind_label": KIND_LABEL[kind],
                "severity": _severity(score),
                "severity_score": score,
                "causes": causes,
                "affected": affected[:8],
                "summary": f"{article} {label} in {path} reaches {len(affected)} {noun}. {detail}.",
            }
        )
    return traces


def _causes(module: dict, signals: dict[str, list[dict]], reverse: dict, cycles: set[str], by_path: dict) -> list[dict]:
    causes = []
    seen = set()
    for item in module.get("evidence") or []:
        spec = VULNERABILITY.get(item.get("kind") or "")
        if spec is None or item.get("kind") in seen:
            continue
        seen.add(item["kind"])
        weight, label = spec
        causes.append({"kind": "vulnerability", "label": label, "weight": weight})
    for raw in module.get("external_imports") or []:
        name = _pkg_name(str(raw))
        for signal in signals.get(name) or []:
            if signal["key"] == "failure":
                causes.append({"kind": "failure", "label": f"{signal['label']} through {name}", "weight": FAILURE})
            elif signal["key"] == "upgrade":
                causes.append(
                    {"kind": "dependency_change", "label": f"{signal['label']} through {name}", "weight": UPGRADE}
                )
    other_services = set()
    for importer in reverse.get(module["path"]) or []:
        other = by_path.get(importer)
        service = other["service"] if other else _service_of(importer)
        if service and service != module["service"]:
            other_services.add(service)
    if module["path"] in cycles:
        causes.append({"kind": "architecture", "label": "This file sits on an import cycle", "weight": 0.30})
    elif len(other_services) >= 2:
        names = ", ".join(sorted(other_services))
        causes.append({"kind": "architecture", "label": f"Imported across services: {names}", "weight": 0.22})
    return causes


def _service_of(path: str) -> str:
    parts = path.split("/")
    if len(parts) > 1 and parts[0] in {"services", "libs", "clients"}:
        return parts[1] if parts[0] != "clients" else "client"
    return parts[0] if parts else ""


def _walk(starts: list[str], graph: dict[str, list[str]]) -> tuple[dict[str, int], dict[str, str | None]]:
    dist = {node: 0 for node in starts}
    parent: dict[str, str | None] = {node: None for node in starts}
    queue: deque[str] = deque(starts)
    while queue:
        node = queue.popleft()
        if dist[node] >= MAX_DEPTH:
            continue
        for nxt in graph.get(node, []):
            if nxt not in dist:
                dist[nxt] = dist[node] + 1
                parent[nxt] = node
                queue.append(nxt)
    return dist, parent


def _chain(path: str, parent: dict[str, str | None]) -> list[str]:
    chain = [path]
    seen = set()
    while parent.get(chain[-1]) and chain[-1] not in seen:
        seen.add(chain[-1])
        chain.append(parent[chain[-1]])
    chain.reverse()
    return chain


def _severity(score: float) -> str:
    if score >= 0.6:
        return "critical"
    if score >= 0.4:
        return "high"
    if score >= 0.2:
        return "medium"
    return "low"


def _consequence(module: str, depth: int, parent: str, by_path: dict, chain: list[str]) -> str:
    if depth == 0:
        sentence = "The weakness starts in this file."
    else:
        sentence = f"{module} imports {parent}."
        if by_path[module]["service"] != by_path[parent]["service"]:
            sentence += f" The import crosses {by_path[parent]['service']} into {by_path[module]['service']}."
    label = CAPABILITY.get(_stem(module))
    if label:
        sentence += f" The file name marks it as {label}."
    if depth > 1:
        sentence += " Path: " + " → ".join(chain) + "."
    return sentence


def _stem(module: str) -> str:
    return module.rsplit("/", 1)[-1].split(".", 1)[0].lower()


def _age(value) -> str:
    number = float(value or 0)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")
