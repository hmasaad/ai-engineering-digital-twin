"""Blast radius of a proposed change against the current twin.

The import walk says which modules would notice the edit. The engineering
impact follows that edit through tests, packages, native plugins, and a
recorded build failure. A path is evidence from the graph, not proof the
change is safe.
"""

from __future__ import annotations

from collections import Counter

from app.graph import adjacency, reachable
from app.temporal import holds


def assess_change(view: dict, modules: list[str], title: str = "Proposed change", note: str = "") -> dict:
    by_path = {module["path"]: module for module in view["modules"]}
    missing = [path for path in modules if path not in by_path or by_path[path]["role"] != "code"]
    if missing:
        raise KeyError(f"Unknown module: {', '.join(missing)}")
    if not modules:
        raise KeyError("Select at least one module")

    depths = reachable(modules, adjacency(view["edges"], reverse=True), max_depth=3)
    forward = adjacency(view["edges"], reverse=False)
    blast = []
    for path, depth in sorted(depths.items(), key=lambda item: (item[1], item[0])):
        module = by_path[path]
        blast.append(
            {
                "module": path,
                "service": module["service"],
                "depth": depth,
                "bug": module["risks"]["bug"],
                "delay": module["risks"]["delay"],
                "test_gap": module["test_gap"],
                "relation": "changed" if depth == 0 else "dependent",
            }
        )

    survival = 1.0
    for item in blast:
        exposure = 1.0 if item["depth"] == 0 else 0.55 / item["depth"]
        local = item["bug"] * (0.4 + 0.6 * item["test_gap"]) * exposure
        survival *= 1.0 - max(0.0, min(0.9, local))
    touched = {item["module"] for item in blast}
    objections = [
        {
            "agent": finding["agent"],
            "severity": finding["severity"],
            "title": finding["title"],
            "remediation": finding["remediation"],
        }
        for finding in view["findings"]
        if finding["severity"] in {"critical", "high"} and set(finding["modules"]) & touched
    ][:4]
    missing_tests = [item["module"] for item in blast if item["depth"] == 0 and item["test_gap"] >= 0.5]
    author_counts: Counter[str] = Counter()
    for item in blast:
        if item["depth"] > 1:
            continue
        for author in by_path[item["module"]]["authors"]:
            author_counts[author] += 1
    contracts = []
    for path in modules:
        for dependency in forward.get(path, []):
            contracts.append({"module": dependency, "via": path})

    direct = sum(1 for item in blast if item["depth"] == 0)
    indirect = len(blast) - direct
    sentences = []
    if missing_tests:
        sentences.append(f"Add or update tests for {', '.join(missing_tests)} before merging.")
    if objections:
        sentences.append(f"{objections[0]['agent']} objects: {objections[0]['title']}.")
    sentences.append(f"The change touches {direct} module{'s' if direct != 1 else ''} directly and {indirect} more through imports.")
    impact = engineering_impact(view, modules)
    if any(path["kind"] == "failure" for path in impact["paths"]):
        sentences.append(impact["summary"])
    if note:
        sentences.append(note)

    return {
        "title": title,
        "note": note,
        "risk": round(1.0 - survival, 4),
        "delay_pressure": round(max(item["delay"] for item in blast if item["depth"] == 0), 4),
        "blast": blast,
        "missing_tests": missing_tests,
        "reviewers": [name for name, _count in author_counts.most_common(4)],
        "contracts": contracts,
        "objections": objections,
        "recommendation": " ".join(sentences),
        "impact": impact,
    }


def engineering_impact(view: dict, modules: list[str]) -> dict:
    """Tests, packages, plugins, and build failures tied to the edited files."""
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    by_path = {module["path"]: module for module in view["modules"]}
    selected = set(modules)
    declared = set((view.get("project") or {}).get("dependencies") or [])
    paths: list[dict] = []

    for fact in facts:
        if fact.get("predicate") != "modifies" or fact.get("object") not in selected:
            continue
        paths.append(_path("pull_request", fact, "modified this file"))

    pull_requests = {path["subject"] for path in paths if path["kind"] == "pull_request"}
    seen_tests: set[str] = set()
    for fact in facts:
        covers = fact.get("predicate") == "covers" and fact.get("object") in selected and holds(fact, 0)
        named = fact.get("predicate") == "affects" and fact.get("subject") in pull_requests
        if not covers and not named:
            continue
        test = str(fact["subject"] if covers else fact["object"])
        if test in seen_tests:
            continue
        seen_tests.add(test)
        paths.append(_path("test", fact, "test imports this file" if covers else "pull request named this test"))

    build_ids = {
        fact["subject"]
        for fact in facts
        if fact.get("predicate") == "affects"
        and str(fact.get("subject") or "").startswith("build:")
        and fact.get("object") in seen_tests
    }
    attributed: dict[str, str] = {}
    for fact in facts:
        if fact.get("predicate") == "attributed_to" and fact.get("subject") in build_ids:
            attributed[fact["subject"]] = str(fact.get("object") or "")
    for fact in facts:
        if fact.get("predicate") != "failed" or fact.get("subject") not in build_ids:
            continue
        package = attributed.get(fact["subject"]) or str(fact.get("object") or "failure")
        row = _path("failure", fact, "attributed to this package" if fact["subject"] in attributed else "build failure")
        row["object"] = package
        paths.append(row)

    package_names, via_import = _packages_used(by_path, modules)
    package_names.update(name for name in attributed.values() if name)
    pinned: set[str] = set()
    for fact in facts:
        if fact.get("predicate") != "depends_on" or not holds(fact, 0):
            continue
        name = _pkg_name(str(fact.get("object") or ""))
        if name not in package_names:
            continue
        pinned.add(name)
        row = _path("package", fact, via_import.get(name) or "recorded pin")
        paths.append(row)
    for name in sorted(package_names - pinned):
        if name not in declared and name not in attributed.values():
            continue
        paths.append(
            {
                "kind": "package",
                "subject": "manifest",
                "object": name,
                "via": via_import.get(name) or "named by a build failure",
                "summary": "Declared in the manifest. No pin interval was logged.",
                "when": "at the scan",
            }
        )
    for fact in facts:
        if fact.get("predicate") != "plugin" or not holds(fact, 0):
            continue
        if fact.get("subject") not in pinned and fact.get("subject") not in package_names:
            continue
        paths.append(_path("plugin", fact, "package binds this plugin"))

    unique: list[dict] = []
    seen: set[tuple] = set()
    order = {"pull_request": 0, "test": 1, "failure": 2, "package": 3, "plugin": 4}
    for row in sorted(paths, key=lambda item: (order.get(item["kind"], 9), item["subject"], item["object"])):
        key = (row["kind"], row["subject"], row["object"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique = unique[:12]
    return {"paths": unique, "summary": _impact_summary(unique)}


def _packages_used(by_path: dict, modules: list[str]) -> tuple[set[str], dict[str, str]]:
    names: set[str] = set()
    via: dict[str, str] = {}
    for path in modules:
        module = by_path.get(path) or {}
        for raw in module.get("external_imports") or []:
            name = _pkg_name(str(raw))
            names.add(name)
            via.setdefault(name, "imported by this file")
        for imported in module.get("imports") or []:
            other = by_path.get(imported) or {}
            for raw in other.get("external_imports") or []:
                name = _pkg_name(str(raw))
                if name in via:
                    continue
                names.add(name)
                via[name] = f"imported through {imported}"
    return names, via


def _path(kind: str, fact: dict, via: str) -> dict:
    return {
        "kind": kind,
        "subject": str(fact.get("subject") or ""),
        "object": str(fact.get("object") or ""),
        "via": via,
        "summary": fact.get("summary") or "",
        "when": _when(fact),
    }


def _when(fact: dict) -> str:
    if fact.get("kind") == "event":
        return f"{_age(fact.get('at_age'))}d ago"
    if fact.get("to_age") is None:
        return f"since {_age(fact.get('from_age'))}d ago"
    return f"{_age(fact.get('from_age'))}d ago until {_age(fact.get('to_age'))}d ago"


def _age(value) -> str:
    number = float(value or 0)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _pkg_name(label: str) -> str:
    if label.startswith("@") and label.count("@") >= 2:
        return label.rsplit("@", 1)[0]
    if "@" in label and not label.startswith("@"):
        return label.split("@", 1)[0]
    return label


def _impact_summary(paths: list[dict]) -> str:
    if not paths:
        return "No package, plugin, test, or recorded build failure is tied to these files."
    parts = []
    tests = []
    for path in paths:
        if path["kind"] != "test":
            continue
        tests.append(path["subject"] if path["via"] == "test imports this file" else path["object"])
    if tests:
        parts.append("Tests in the impact: " + ", ".join(dict.fromkeys(tests)) + ".")
    failure = next((path for path in paths if path["kind"] == "failure"), None)
    if failure:
        text = failure["summary"] or f"{failure['subject']} failed."
        if failure["object"] and failure["object"] not in {"failure", "result"}:
            text = f"{text} That failure is associated with {failure['object']}."
        parts.append(text)
    package = next((path for path in paths if path["kind"] == "package"), None)
    if package:
        parts.append(f"The change sits on {package['object']} ({package['when']}).")
    plugin = next((path for path in paths if path["kind"] == "plugin"), None)
    if plugin:
        parts.append(f"{plugin['subject']} binds {plugin['object']}.")
    if not parts:
        pull = next(path for path in paths if path["kind"] == "pull_request")
        parts.append(pull["summary"] or f"{pull['subject']} modified {pull['object']}.")
    return " ".join(parts)
