"""Eight change-impact algorithms, each reading the step before it.

The static graph is a breadth-first walk of import edges. Transitive impact
uses that walk and the blast-radius exposure. Coupling counts files that share
a commit, or that the same author changed within 14 days. Semantic reading
lists definitions, routes, and the latest commit. The impact figure on each
downstream module is an index with a band. Test selection is a greedy cover of
that pressure. Uncertainty widens the same change risk index. The explanation
cites the edge, co-change, test, or index it used.
"""

from __future__ import annotations

from collections import deque

from app.change_engine import _commits, _read_source, _symbols, blast_risk
from app.graph import adjacency, find_cycles
from app.temporal import holds

WINDOW_DAYS = 14.0
MAX_DEPTH = 3


def run_algorithms(view: dict, modules: list[str], report: dict) -> dict:
    static = _static_graph(view, modules)
    propagation = _propagate(static, report)
    coupling = _coupling(view, modules)
    semantic = _understand(view, modules, report, static)
    prediction = _predict(report, coupling)
    tests = _select_tests(view, modules, report, prediction)
    uncertainty = _uncertainty(view, modules, report, coupling)
    explanation = _explain(report, static, coupling, semantic, prediction, tests, uncertainty)
    steps = [static, propagation, coupling, semantic, prediction, tests, uncertainty, explanation]
    return {
        "steps": steps,
        "note": (
            "The steps run in order. The graph walk feeds propagation. "
            "Coupling and the file text feed the reading and the impact bands. "
            "Test selection covers the highest pressure first. "
            "The change risk index stays the blast-radius figure, and its band widens when tests or history are thin. "
            "An impact index is not a probability the module will fail."
        ),
    }


def _step(order: int, title: str, summary: str, rows: list[dict]) -> dict:
    return {"order": order, "title": title, "summary": summary, "rows": rows}


def _static_graph(view: dict, modules: list[str]) -> dict:
    by_path = {module["path"]: module for module in view["modules"]}
    reverse = adjacency(view.get("edges") or [], reverse=True)
    forward = adjacency(view.get("edges") or [], reverse=False)
    incoming = _walk(modules, reverse, "importer")
    outgoing = _walk(modules, forward, "dependency")
    edges = incoming["edges"] + outgoing["edges"]
    direct = sum(1 for edge in edges if edge["depth"] == 1)
    transitive = len(edges) - direct
    rows = [
        {
            "title": f"{edge['source']} imports {edge['target']}",
            "detail": f"{'direct' if edge['depth'] == 1 else 'transitive'} · depth {edge['depth']} · {edge['direction']}",
        }
        for edge in edges[:8]
    ]
    visited = set(modules) | incoming["reached"] | outgoing["reached"]
    cycles = [cycle for cycle in find_cycles(view.get("edges") or []) if visited.intersection(cycle)]
    if cycles:
        rows.append(
            {
                "title": " → ".join(cycles[0]),
                "detail": f"cycle through the visited files · {len(cycles)} found",
            }
        )
    known = sum(1 for path in visited if path in by_path)
    summary = (
        f"Visited {known} modules and {len(edges)} import edges. "
        f"{direct} are direct and {transitive} are transitive, out to depth {MAX_DEPTH}."
    )
    if not edges:
        summary = "The selected files have no import edges within depth 3."
    step = _step(1, "Static dependency analysis and graph traversal", summary, rows)
    step["direct"] = direct
    step["transitive"] = transitive
    return step


def _walk(starts: list[str], graph: dict[str, list[str]], direction: str) -> dict:
    dist = {node: 0 for node in starts}
    parent: dict[str, str | None] = {node: None for node in starts}
    edges = []
    queue: deque[str] = deque(starts)
    while queue:
        node = queue.popleft()
        if dist[node] >= MAX_DEPTH:
            continue
        for nxt in graph.get(node, []):
            if nxt in dist:
                continue
            dist[nxt] = dist[node] + 1
            parent[nxt] = node
            if direction == "importer":
                edges.append({"source": nxt, "target": node, "depth": dist[nxt], "direction": direction})
            else:
                edges.append({"source": node, "target": nxt, "depth": dist[nxt], "direction": direction})
            queue.append(nxt)
    reached = {path for path, depth in dist.items() if depth > 0}
    return {"edges": edges, "reached": reached, "dist": dist, "parent": parent}


def _propagate(static: dict, report: dict) -> dict:
    _risk, parts = blast_risk(report["blast"])
    by_module = {item["module"]: item for item in parts}
    rows = []
    for item in sorted(
        (row for row in report["blast"] if row["depth"] > 0),
        key=lambda row: (-by_module[row["module"]]["pressure"], row["depth"], row["module"]),
    )[:8]:
        part = by_module[item["module"]]
        rows.append(
            {
                "title": item["module"],
                "detail": (
                    f"{'direct' if item['depth'] == 1 else 'transitive'} · depth {item['depth']} · "
                    f"exposure {part['exposure']:.2f} · pressure {part['pressure']:.2f}"
                ),
            }
        )
    if not rows:
        summary = "No module imports the edit, so nothing is propagated."
    else:
        lead = rows[0]
        summary = (
            f"Pressure follows the {static.get('direct', 0)} direct and {static.get('transitive', 0)} transitive edges. "
            f"The largest is {by_module[lead['title']]['pressure']:.2f} on {lead['title']}."
        )
    return _step(2, "Transitive dependency and impact propagation", summary, rows)


def _coupling(view: dict, modules: list[str]) -> dict:
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    groups: dict[tuple, set[str]] = {}
    for fact in facts:
        if fact.get("predicate") != "changed" or not fact.get("object"):
            continue
        key = (
            str(fact.get("subject") or ""),
            round(float(fact.get("at_age") or 0), 2),
            str(fact.get("summary") or ""),
        )
        groups.setdefault(key, set()).add(str(fact["object"]))
    selected = set(modules)
    selected_keys = [key for key, files in groups.items() if files & selected]
    scores: dict[str, dict] = {}

    def add(path: str, weight: float, author: str, kind: str) -> None:
        if path in selected:
            return
        bucket = scores.setdefault(path, {"score": 0.0, "links": 0, "author": author, "kind": kind})
        bucket["score"] += weight
        bucket["links"] += 1
        if kind == "same commit":
            bucket["kind"] = kind

    for key, files in groups.items():
        if not files & selected:
            continue
        author, _age, _summary = key
        for path in files:
            add(path, 1.0, author, "same commit")
    for key in selected_keys:
        author, age, _summary = key
        for other_key, files in groups.items():
            if other_key == key or other_key[0] != author:
                continue
            if abs(other_key[1] - age) > WINDOW_DAYS:
                continue
            for path in files:
                add(path, 0.5, author, f"{int(WINDOW_DAYS)}-day window")
    ranked = sorted(scores.items(), key=lambda item: (-item[1]["score"], item[0]))
    touched = sum(1 for key in selected_keys if _commit_has_partner(key, groups, selected))
    support = touched / len(selected_keys) if selected_keys else 0.0
    rows = [
        {
            "title": path,
            "detail": (
                f"{info['links']} links · {info['kind']} · {info['author']} · score {info['score']:.2f}"
            ),
        }
        for path, info in ranked[:8]
    ]
    if not rows:
        summary = "No other file shares a commit with these files, and none changed under the same author within 14 days."
    else:
        lead, info = ranked[0]
        summary = (
            f"{lead} is the strongest co-change, score {info['score']:.2f}, from {info['links']} {info['kind']} links"
            f" by {info['author']}. Support is {support:.2f} across {len(selected_keys)} recorded commits."
        )
    return _step(3, "Change coupling analysis", summary, rows)


def _commit_has_partner(key: tuple, groups: dict, selected: set[str]) -> bool:
    files = groups[key]
    if any(path not in selected for path in files):
        return True
    author, age, _summary = key
    for other_key, other_files in groups.items():
        if other_key == key or other_key[0] != author or abs(other_key[1] - age) > WINDOW_DAYS:
            continue
        if any(path not in selected for path in other_files):
            return True
    return False


def _understand(view: dict, modules: list[str], report: dict, static: dict) -> dict:
    by_path = {module["path"]: module for module in view["modules"]}
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    commits = _commits(facts, modules)
    importers = [
        row["title"].split(" imports ", 1)[0]
        for row in static["rows"]
        if " · importer" in f" {row['detail']}" and row["detail"].startswith("direct")
    ]
    kinds = []
    rows = []
    sentences = []
    for path in modules[:2]:
        parsed = _symbols(_read_source(view, path), (by_path.get(path) or {}).get("language") or "")
        names = [item["name"] for item in parsed["api"][:3]]
        extra = len(parsed["api"]) - len(names)
        if names:
            listed = ", ".join(names) + (f", and {extra} more" if extra else "")
            if extra == 0 and len(names) > 1:
                listed = _join(names)
            sentences.append(f"{path} defines {listed}.")
            rows.append({"title": names[0], "detail": f"defined in {path}"})
            kinds.append("shared behavior" if importers else "local behavior")
        elif parsed["classes"]:
            sentences.append(f"{path} defines the class {parsed['classes'][0]['name']}.")
            rows.append({"title": parsed["classes"][0]["name"], "detail": f"class in {path}"})
            kinds.append("local behavior")
        routes = [item["path"] for item in parsed["routes"][:3]]
        if routes:
            extra_routes = len(parsed["routes"]) - len(routes)
            listed_routes = ", ".join(routes) + (f", and {extra_routes} more" if extra_routes else "")
            sentences.append(f"It calls {listed_routes}.")
            rows.append({"title": routes[0], "detail": f"API call in {path}"})
            kinds.append("remote API client")
        commit = commits.get(path)
        if commit:
            sentences.append(f"The latest recorded commit says “{commit['summary']}” ({_ago(commit['age'])}).")
            rows.append({"title": commit["summary"], "detail": f"{_ago(commit['age'])} · {commit['author']}"})
    packages = [
        path_row.get("object")
        for path_row in (report.get("impact") or {}).get("paths") or []
        if path_row.get("kind") == "package" and path_row.get("object")
    ]
    if packages:
        shown = list(dict.fromkeys(packages))[:2]
        sentences.append(f"The edit reaches {' and '.join(shown)}.")
        kinds.append("dependency configuration")
    if importers:
        sentences.append(f"{_join(importers[:3])} import the edit.")
    if not sentences:
        sentences.append("The file text has no parsed function, class, or route.")
        kinds.append("local module")
    kind = kinds[0] if kinds else "local module"
    if "remote API client" in kinds:
        kind = "remote API client"
    elif "shared behavior" in kinds:
        kind = "shared behavior"
    summary = f"Reading: {kind}. " + " ".join(sentences)
    return _step(4, "Semantic code change understanding", summary, rows[:8])


def _predict(report: dict, coupling: dict) -> dict:
    coupled = {row["title"] for row in coupling["rows"]}
    _risk, parts = blast_risk(report["blast"])
    by_module = {item["module"]: item for item in parts}
    scored = []
    for item in report["blast"]:
        if item["depth"] == 0:
            continue
        part = by_module[item["module"]]
        index = part["pressure"]
        width = 0.16
        if item["depth"] <= 1:
            width -= 0.04
        if item["test_gap"] < 0.5:
            width -= 0.04
        if item["module"] in coupled:
            width -= 0.02
        width = min(0.24, max(0.06, width))
        low = round(max(0.0, index - width), 4)
        high = round(min(1.0, index + width), 4)
        confidence = "high" if width <= 0.08 else "medium" if width <= 0.12 else "low"
        scored.append(
            {
                "module": item["module"],
                "index": round(index, 4),
                "low": low,
                "high": high,
                "width": round(width, 4),
                "confidence": confidence,
                "depth": item["depth"],
            }
        )
    scored.sort(key=lambda item: (-item["index"], item["depth"], item["module"]))
    rows = [
        {
            "title": item["module"],
            "detail": f"impact index {item['index']:.2f} · band {item['low']:.2f}–{item['high']:.2f} · {item['confidence']}",
        }
        for item in scored[:8]
    ]
    if not scored:
        summary = "No downstream module has an impact index."
    else:
        lead = scored[0]
        summary = (
            f"The highest impact index is {lead['index']:.2f} on {lead['module']}, "
            f"band {lead['low']:.2f}–{lead['high']:.2f} ({lead['confidence']}). "
            "The index is the local blast pressure. The band widens with depth and a missing test, and narrows when the file also co-changed."
        )
    return _step(5, "Probabilistic impact prediction", summary, rows)


def _select_tests(view: dict, modules: list[str], report: dict, prediction: dict) -> dict:
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    blast = {item["module"]: item for item in report["blast"]}
    _risk, parts = blast_risk(report["blast"])
    pressure = {item["module"]: item["pressure"] for item in parts}
    covers: dict[str, set[str]] = {}
    for fact in facts:
        if fact.get("predicate") != "covers" or not holds(fact, 0):
            continue
        target = str(fact.get("object") or "")
        if target not in blast:
            continue
        covers.setdefault(str(fact.get("subject") or ""), set()).add(target)
    chosen = []
    remaining = set(blast)
    unused = dict(covers)
    while unused and remaining:
        best = max(unused, key=lambda test: (sum(pressure.get(path, 0.0) for path in unused[test] & remaining), test))
        value = sum(pressure.get(path, 0.0) for path in unused[best] & remaining)
        if value <= 0:
            break
        covered = unused.pop(best) & remaining
        remaining -= covered
        chosen.append({"title": best, "detail": f"covers {', '.join(sorted(covered))} · pressure {value:.2f}"})
    named = []
    for item in (report.get("analysis") or {}).get("tests") or []:
        if item["path"] in covers or any(row["title"] == item["path"] for row in chosen):
            continue
        place = "in the tree" if item.get("in_tree") else "named in history, file not in the tree"
        named.append({"title": item["path"], "detail": f"{item.get('reason') or 'test'} · {place}"})
    gaps = []
    ordered_gaps = sorted(remaining, key=lambda path: (0 if path in set(modules) else 1, -pressure.get(path, 0.0), path))
    for path in ordered_gaps[:4]:
        gaps.append({"title": path, "detail": f"write a test · pressure {pressure.get(path, 0.0):.2f}"})
    rows = (chosen + named + gaps)[:8]
    if chosen:
        summary = "Run " + _join(row["title"] for row in chosen) + "."
    else:
        summary = "No in-tree test covers this blast."
    if gaps:
        summary += f" The first test to add is {gaps[0]['title']}."
    if named:
        summary += f" Also recorded: {_join(row['title'] for row in named)}."
    return _step(6, "Test selection optimization", summary, rows)


def _uncertainty(view: dict, modules: list[str], report: dict, coupling: dict) -> dict:
    risk, _parts = blast_risk(report["blast"])
    blast = report["blast"]
    untested = sum(1 for item in blast if item["test_gap"] >= 0.5) / len(blast) if blast else 1.0
    max_depth = max((item["depth"] for item in blast), default=0)
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    selected = set(modules)
    commit_keys = {
        (str(fact.get("subject") or ""), round(float(fact.get("at_age") or 0), 2), str(fact.get("summary") or ""))
        for fact in facts
        if fact.get("predicate") == "changed" and fact.get("object") in selected
    }
    failure = any(path.get("kind") == "failure" for path in (report.get("impact") or {}).get("paths") or [])
    width = 0.08 + 0.08 * untested
    reasons = [f"base 0.08", f"untested share {untested:.2f}"]
    if max_depth >= 3:
        width += 0.04
        reasons.append("a hop reaches depth 3")
    if len(commit_keys) < 3:
        width += 0.04
        reasons.append("fewer than 3 recorded commits on the edit")
    if failure:
        width -= 0.02
        reasons.append("a recorded build failure is on the path")
    if coupling["rows"]:
        width -= 0.01
        reasons.append("a co-change was found")
    width = min(0.24, max(0.06, width))
    low = round(max(0.0, risk - width), 4)
    high = round(min(1.0, risk + width), 4)
    rows = [
        {"title": "Change risk index", "detail": f"{risk:.2f} · band {low:.2f}–{high:.2f}"},
        {"title": "Band width", "detail": f"{width:.2f} · " + "; ".join(reasons)},
    ]
    summary = (
        f"The change risk index is {risk:.2f}, band {low:.2f}–{high:.2f}. "
        "The point is the blast-radius index. The band widens when the blast is untested or the history is thin."
    )
    return _step(7, "Uncertainty-aware risk scoring", summary, rows)


def _explain(report, static, coupling, semantic, prediction, tests, uncertainty) -> dict:
    coupled = {row["title"]: row["detail"] for row in coupling["rows"]}
    bands = {}
    for row in prediction["rows"]:
        bands[row["title"]] = row["detail"]
    edge_for = {}
    for row in static["rows"]:
        if " imports " not in row["title"]:
            continue
        source, target = row["title"].split(" imports ", 1)
        if row["detail"].endswith("importer"):
            edge_for.setdefault(source, f"{source} imports {target}")
    rows = []
    for item in report["blast"][:8]:
        bits = []
        if item["depth"] == 0:
            bits.append("Selected in the proposal.")
            if semantic["rows"]:
                bits.append(semantic["summary"].split(". ", 1)[0] + ".")
        else:
            edge = edge_for.get(item["module"]) or f"Reached at depth {item['depth']}"
            bits.append(edge if edge.endswith(".") else edge + ".")
        if item["module"] in coupled:
            bits.append(f"Co-change: {coupled[item['module']]}.")
        if item["module"] in bands:
            bits.append(bands[item["module"]] + ".")
        rows.append({"title": item["module"], "detail": " ".join(bits)})
    risk_line = uncertainty["rows"][0]["detail"] if uncertainty["rows"] else ""
    if tests["rows"]:
        rows.append({"title": tests["rows"][0]["title"], "detail": f"Test plan · {tests['rows'][0]['detail']}"})
    if risk_line:
        rows.append({"title": "Change risk index", "detail": risk_line})
    count = sum(1 for item in report["blast"])
    summary = f"{count} components explained from the graph walk, co-changes, impact bands, the test plan, and the risk band."
    return _step(8, "Evidence-based impact explanation", summary, rows[:8])


def _ago(age: float) -> str:
    number = float(age or 0)
    if abs(number - round(number)) < 1e-6:
        return f"{int(round(number))}d ago"
    return f"{number:.2f}".rstrip("0").rstrip(".") + "d ago"


def _join(names) -> str:
    names = list(names)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]
