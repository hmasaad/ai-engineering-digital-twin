"""Propagate dependency risk through the modules that import it.

The index starts at a package: a recorded build failure, a recent upgrade,
a native plugin, or an import the manifest does not declare. Direct importers
take that index. Modules that import those files take a smaller share, out to
three hops. It is a decision-support index and does not move the bug,
incident, delay, or performance scores.
"""

from __future__ import annotations

from app.graph import adjacency, reachable
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
    return {
        "packages": packages[:8],
        "modules": rows[:12],
        "summary": _summary(packages),
        "note": (
            "Dependency risk starts at the package and decays through importers. "
            "A direct import keeps the package index. Each further hop keeps a smaller share, out to depth 3. "
            "This does not move the bug, incident, delay, or performance indexes."
        ),
    }


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


def _age(value) -> str:
    number = float(value or 0)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")
