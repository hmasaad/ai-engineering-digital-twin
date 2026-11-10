"""Blast radius of a proposed change against the current twin.

The import walk says which modules would notice the edit. The engineering
impact follows that edit through tests, packages, native plugins, and a
recorded build failure. The analysis answers which components, dependencies,
APIs, tests, regressions, and delivery risk that walk implies. A path is
evidence from the graph, not proof the change is safe. The change risk index
is the blast-radius figure and is not recomputed here.
"""

from __future__ import annotations

from collections import Counter

from app.change_algorithms import run_algorithms
from app.change_engine import blast_risk, run_engines
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

    risk, _pressures = blast_risk(blast)
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

    report = {
        "title": title,
        "note": note,
        "risk": risk,
        "delay_pressure": round(max(item["delay"] for item in blast if item["depth"] == 0), 4),
        "blast": blast,
        "missing_tests": missing_tests,
        "reviewers": [name for name, _count in author_counts.most_common(4)],
        "contracts": contracts,
        "objections": objections,
        "recommendation": " ".join(sentences),
        "impact": impact,
    }
    report["analysis"] = analyze_change(view, modules, report)
    report["engines"] = run_engines(view, modules, report)
    report["algorithms"] = run_algorithms(view, modules, report)
    return report


def analyze_change(view: dict, modules: list[str], report: dict) -> dict:
    """Six answers about a change, read from the blast and the graph."""
    by_path = {module["path"]: module for module in view["modules"]}
    blast = report["blast"]
    selected = set(modules)
    components = [
        {
            "module": item["module"],
            "service": item["service"],
            "depth": item["depth"],
            "relation": item["relation"],
        }
        for item in blast
    ]
    dependencies = _breakable(view, modules)
    apis = _apis(view, by_path, blast, selected)
    services = _services(blast, apis)
    tests, uncovered = _tests(view, by_path, blast, selected, report["impact"])
    regressions = _regressions(blast)
    delivery = report["delay_pressure"]
    delay_module = _delay_module(blast)
    operational, operational_module = _operational(by_path, blast)
    analysis = {
        "components": components,
        "dependencies": dependencies,
        "services": services,
        "apis": apis,
        "tests": tests,
        "uncovered": uncovered,
        "regressions": regressions,
        "delivery": delivery,
        "delay_module": delay_module,
        "operational": operational,
        "operational_module": operational_module,
        "change_risk": report["risk"],
        "note": (
            "The answers read the blast, the import graph, recorded tests, and the indexes already on the twin. "
            "The change risk index is the blast-radius figure. "
            "A path is evidence from the graph, not proof the change is safe."
        ),
    }
    analysis["answers"] = _answers(analysis)
    return analysis


def _breakable(view: dict, modules: list[str]) -> list[dict]:
    names, via = _packages_used({module["path"]: module for module in view["modules"]}, modules)
    published = {
        item["name"]: item
        for item in ((view.get("dependencies") or {}).get("packages") or [])
        if item.get("name")
    }
    declared = set((view.get("project") or {}).get("dependencies") or [])
    rows = []
    for name in sorted(names):
        record = published.get(name)
        drivers = list((record or {}).get("drivers") or [])
        missing = name not in declared
        if record is None and not missing:
            continue
        if record is not None and not drivers and not missing:
            continue
        if missing and "Imported and missing from the manifest" not in drivers:
            drivers.append("Imported and missing from the manifest")
        rows.append(
            {
                "name": name,
                "version": (record or {}).get("version") or "",
                "risk": None if record is None else record.get("risk"),
                "may_break": True,
                "why": " · ".join(drivers),
                "via": via.get(name) or "",
            }
        )
    rows.sort(key=lambda item: (-(item["risk"] if item["risk"] is not None else -1), item["name"]))
    return rows[:8]


def _apis(view: dict, by_path: dict, blast: list[dict], selected: set[str]) -> list[dict]:
    reached = {item["module"] for item in blast}
    rows = []
    seen: set[tuple[str, str]] = set()
    for edge in view.get("edges") or []:
        caller = edge.get("source")
        callee = edge.get("target")
        if caller not in by_path or callee not in by_path:
            continue
        caller_service = by_path[caller]["service"]
        callee_service = by_path[callee]["service"]
        if caller_service == callee_service:
            continue
        inbound = callee in reached and caller not in selected
        outbound = caller in selected
        if not inbound and not outbound:
            continue
        key = (caller, callee)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "caller": caller,
                "callee": callee,
                "from_service": caller_service,
                "to_service": callee_service,
                "direction": "outbound" if caller in selected else "inbound",
            }
        )
    rows.sort(key=lambda item: (item["direction"] != "inbound", item["caller"], item["callee"]))
    return rows[:12]


def _services(blast: list[dict], apis: list[dict]) -> list[dict]:
    grouped: dict[str, list[str]] = {}
    depth0: set[str] = set()
    for item in blast:
        grouped.setdefault(item["service"], [])
        if item["module"] not in grouped[item["service"]]:
            grouped[item["service"]].append(item["module"])
        if item["depth"] == 0:
            depth0.add(item["service"])
    rows = [
        {
            "name": name,
            "role": "edited" if name in depth0 else "affected",
            "modules": modules,
        }
        for name, modules in grouped.items()
    ]
    present = {row["name"] for row in rows}
    for api in apis:
        if api["direction"] != "outbound" or api["to_service"] in present:
            continue
        present.add(api["to_service"])
        rows.append({"name": api["to_service"], "role": "called", "modules": [api["callee"]]})
    rows.sort(key=lambda item: ({"edited": 0, "affected": 1, "called": 2}[item["role"]], item["name"]))
    return rows


def _tests(
    view: dict,
    by_path: dict,
    blast: list[dict],
    selected: set[str],
    impact: dict,
) -> tuple[list[dict], list[str]]:
    reached = {item["module"] for item in blast}
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    rows: list[dict] = []
    seen: set[str] = set()
    for fact in facts:
        if fact.get("predicate") != "covers" or fact.get("object") not in reached or not holds(fact, 0):
            continue
        path = str(fact.get("subject") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        rows.append(
            {
                "path": path,
                "target": str(fact.get("object") or ""),
                "reason": "imports the affected module",
                "in_tree": path in by_path,
            }
        )
    for path_row in impact.get("paths") or []:
        if path_row.get("kind") != "test":
            continue
        path = path_row["subject"] if path_row.get("via") == "test imports this file" else str(path_row.get("object") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        rows.append(
            {
                "path": path,
                "target": str(path_row.get("object") or "") if path_row.get("via") == "test imports this file" else "",
                "reason": "named by a pull request" if path_row.get("via") != "test imports this file" else "imports the affected module",
                "in_tree": path in by_path,
            }
        )
    rows.sort(key=lambda item: (not item["in_tree"], item["path"]))
    covered = {row["target"] for row in rows if row["in_tree"] and row["target"]}
    uncovered = [
        path
        for path in _edited(blast, selected)
        if by_path[path]["test_gap"] >= 0.5 and path not in covered
    ]
    return rows[:12], uncovered


def _edited(blast: list[dict], selected: set[str]) -> list[str]:
    return [item["module"] for item in blast if item["module"] in selected]


def _regressions(blast: list[dict]) -> list[dict]:
    downstream = [item for item in blast if item["depth"] > 0]
    downstream.sort(key=lambda item: (-item["bug"], item["depth"], item["module"]))
    return [
        {
            "module": item["module"],
            "service": item["service"],
            "depth": item["depth"],
            "bug": item["bug"],
            "test_gap": item["test_gap"],
            "why": "Imports an edited file." if item["depth"] == 1 else "Imports a module this change reaches.",
        }
        for item in downstream[:8]
    ]


def _delay_module(blast: list[dict]) -> str:
    edited = [item for item in blast if item["depth"] == 0]
    if not edited:
        return ""
    return max(edited, key=lambda item: item["delay"])["module"]


def _operational(by_path: dict, blast: list[dict]) -> tuple[float, str]:
    if not blast:
        return 0.0, ""
    lead = max(
        blast,
        key=lambda item: (by_path[item["module"]]["risks"]["incident"], item["module"]),
    )
    score = by_path[lead["module"]]["risks"]["incident"]
    return round(float(score), 4), lead["module"]


def _answers(analysis: dict) -> list[dict]:
    return [
        {"question": "Which components will be affected?", "answer": _component_answer(analysis["components"])},
        {"question": "Which dependencies may break?", "answer": _dependency_answer(analysis["dependencies"])},
        {"question": "Which APIs or services are impacted?", "answer": _api_answer(analysis["services"], analysis["apis"])},
        {
            "question": "Which tests should be executed?",
            "answer": _test_answer(analysis["tests"], analysis["uncovered"]),
        },
        {"question": "Which other modules might experience regressions?", "answer": _regression_answer(analysis)},
        {
            "question": "What is the potential delivery and operational risk?",
            "answer": _risk_answer(analysis),
        },
    ]


def _component_answer(components: list[dict]) -> str:
    edited = [item["module"] for item in components if item["depth"] == 0]
    downstream = [item["module"] for item in components if item["depth"] > 0]
    sentence = f"The edit touches {_join(edited)}."
    if not downstream:
        return sentence + " No other module imports these files."
    shown = downstream[:5]
    extra = len(downstream) - len(shown)
    if extra:
        tail = ", ".join(shown) + f", and {extra} more"
    else:
        tail = _join(shown)
    return f"{sentence} Importers that can see it: {tail}."


def _dependency_answer(dependencies: list[dict]) -> str:
    if not dependencies:
        return "No package used by these files has a recorded failure, upgrade, native plugin, or undeclared import."
    bits = []
    for item in dependencies:
        version = f" {item['version']}" if item.get("version") else ""
        why = item["why"] or "carries a dependency signal"
        bits.append(f"{item['name']}{version} ({why})")
    return "These dependencies may break: " + "; ".join(bits) + "."


def _api_answer(services: list[dict], apis: list[dict]) -> str:
    edited = [item["name"] for item in services if item["role"] == "edited"]
    affected = [item["name"] for item in services if item["role"] == "affected"]
    called = [item["name"] for item in services if item["role"] == "called"]
    sentence = f"Services on the edit: {_join(edited) or 'none'}."
    if affected:
        sentence += f" Services downstream: {_join(affected)}."
    if called:
        sentence += f" The edit calls {_join(called)}."
    if not apis:
        return sentence + " No cross-service call leaves or enters the blast."
    shown = apis[:6]
    calls = "; ".join(f"{item['caller']} calls {item['callee']}" for item in shown)
    extra = len(apis) - len(shown)
    if extra:
        calls = f"{calls}; and {extra} more"
    return f"{sentence} Cross-service calls: {calls}."


def _test_answer(tests: list[dict], uncovered: list[str]) -> str:
    if tests:
        bits = []
        for item in tests:
            place = "in the tree" if item["in_tree"] else "named in history, and the file is not in the tree"
            bits.append(f"{item['path']} ({item['reason']}, {place})")
        sentence = "Run " + "; ".join(bits) + "."
    else:
        sentence = "No covering test and no recorded test name is tied to this change."
    if uncovered:
        sentence += f" No test covers {_join(uncovered)}."
    return sentence


def _regression_answer(analysis: dict) -> str:
    downstream = [item for item in analysis["components"] if item["depth"] > 0]
    regressions = analysis["regressions"]
    if not regressions:
        return "No importer sits downstream of the edit."
    bits = []
    for item in regressions[:5]:
        coverage = ", untested" if item["test_gap"] >= 0.5 else ""
        bits.append(f"{item['module']} (bug {item['bug']:.2f}{coverage})")
    sentence = "Downstream modules, highest bug index first: " + "; ".join(bits)
    extra = len(downstream) - len(regressions[:5])
    if extra > 0:
        sentence += f"; and {extra} more"
    return sentence + "."


def _risk_answer(analysis: dict) -> str:
    return (
        f"Delivery delay index on the edited files is {analysis['delivery']:.2f} on {analysis['delay_module']}. "
        f"The highest incident index in the blast is {analysis['operational']:.2f} on {analysis['operational_module']}. "
        f"The change risk index is {analysis['change_risk']:.2f}. "
        "These are the indexes already on the twin."
    )


def _join(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]


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
