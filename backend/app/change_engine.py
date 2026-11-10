"""Eight engines for one proposed change.

Detection reads the selected files. Traversal walks the import graph.
Propagation and risk scoring use the blast-radius exposure already in the
change risk index. Semantic reading lists definitions, routes, and the latest
commit text. Tests, explanations, and provenance cite the graph, the file, or
that index. None of this edits the repository.
"""

from __future__ import annotations

import ast
import re
from collections import deque
from pathlib import Path

from app.graph import adjacency
from app.settings import MAX_FILE_BYTES

CONST_FN = re.compile(
    r"(?m)^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
FUNC_RE = re.compile(r"(?m)^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")
CLASS_RE = re.compile(r"(?m)^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")
EXPORT_BLOCK = re.compile(r"(?m)^export\s*\{([^}]*)\}", re.S)
ROUTE_RE = re.compile(
    r"""(?:axios\.(?:get|post|put|patch|delete)|fetch)\(\s*['\"]([^'\"]+)['\"]"""
)


def blast_risk(blast: list[dict]) -> tuple[float, list[dict]]:
    """The change risk index. Exposure is 1 on an edited file and 0.55/depth after that."""
    survival = 1.0
    parts = []
    for item in blast:
        exposure = 1.0 if item["depth"] == 0 else 0.55 / item["depth"]
        local = item["bug"] * (0.4 + 0.6 * item["test_gap"]) * exposure
        clamped = max(0.0, min(0.9, local))
        survival *= 1.0 - clamped
        parts.append(
            {
                "module": item["module"],
                "depth": item["depth"],
                "exposure": round(exposure, 4),
                "pressure": round(clamped, 4),
            }
        )
    return round(1.0 - survival, 4), parts


def run_engines(view: dict, modules: list[str], report: dict) -> dict:
    by_path = {module["path"]: module for module in view["modules"]}
    texts = {path: _read_source(view, path) for path in modules}
    symbols = {
        path: _symbols(texts.get(path) or "", (by_path.get(path) or {}).get("language") or "")
        for path in modules
    }
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    commits = _commits(facts, modules)
    hops = _hops(view, modules)
    detection = _detection(modules, by_path, symbols, commits, report)
    traversal = _traversal(hops)
    propagation = _propagation(report["blast"])
    semantic = _semantic(modules, symbols, commits, hops)
    tests = _test_engine(report.get("analysis") or {})
    risk = _risk_engine(report, by_path)
    explanation = _explanation(report["blast"], hops)
    provenance = _provenance(modules, symbols, commits, hops, report, risk)
    return {
        "detection": detection,
        "traversal": traversal,
        "propagation": propagation,
        "semantic": semantic,
        "tests": tests,
        "risk": risk,
        "explanation": explanation,
        "provenance": provenance,
        "note": (
            "Change detection reads the selected files. "
            "Traversal and propagation follow imports out to depth 3. "
            "Risk scoring is the blast-radius index. "
            "Each cited row names the file, edge, test, commit, or index it came from."
        ),
    }


def _detection(modules, by_path, symbols, commits, report) -> dict:
    rows = []
    functions = 0
    classes = 0
    routes = 0
    for path in modules:
        module = by_path[path]
        parsed = symbols.get(path) or _empty_symbols()
        functions += len(parsed["functions"])
        classes += len(parsed["classes"])
        routes += len(parsed["routes"])
        commit = commits.get(path)
        history = f" · last recorded change {_ago(commit['age'])}, {commit['summary']}" if commit else ""
        rows.append(
            {
                "title": path,
                "detail": f"{module['language']} file · {module['service']}{history}",
            }
        )
        for item in parsed["classes"][:4]:
            rows.append(
                {
                    "title": item["name"],
                    "detail": f"class · line {item['line']} · {path}",
                }
            )
        shown = parsed["api"][:6]
        for item in shown:
            rows.append(
                {
                    "title": item["name"],
                    "detail": f"{item['kind']} · line {item['line']} · {path}",
                }
            )
        for item in parsed["routes"][:4]:
            rows.append(
                {
                    "title": item["path"],
                    "detail": f"API call · line {item['line']} · {path}",
                }
            )
    for path_row in (report.get("impact") or {}).get("paths") or []:
        if path_row.get("kind") not in {"package", "plugin"}:
            continue
        rows.append(
            {
                "title": path_row.get("object") or path_row.get("subject") or "configuration",
                "detail": f"{path_row['kind']} · {path_row.get('via') or 'manifest'} · {path_row.get('when') or ''}".strip(),
            }
        )
    bits = [f"{len(modules)} file" if len(modules) == 1 else f"{len(modules)} files"]
    if functions:
        bits.append(f"{functions} function{'s' if functions != 1 else ''}")
    if classes:
        bits.append(f"{classes} class{'es' if classes != 1 else ''}")
    if routes:
        bits.append(f"{routes} API call{'s' if routes != 1 else ''}")
    configs = [row["title"] for row in rows if row["detail"].startswith("package") or row["detail"].startswith("plugin")]
    if configs:
        bits.append("configuration " + ", ".join(dict.fromkeys(configs)))
    return {"summary": "Detected " + ", ".join(bits) + ".", "rows": rows[:12]}


def _traversal(hops: dict) -> dict:
    rows = []
    for bucket, label in (
        ("direct_importers", "direct importer"),
        ("indirect_importers", "indirect importer"),
        ("direct_dependencies", "direct dependency"),
        ("indirect_dependencies", "indirect dependency"),
    ):
        for item in hops[bucket]:
            via = f" · via {item['via']}" if item.get("via") else ""
            rows.append(
                {
                    "title": item["module"],
                    "detail": f"{label} · depth {item['depth']}{via} · {item['service']}",
                }
            )
    direct = len(hops["direct_importers"]) + len(hops["direct_dependencies"])
    indirect = len(hops["indirect_importers"]) + len(hops["indirect_dependencies"])
    summary = f"{direct} direct and {indirect} indirect import links, out to depth 3."
    if not direct and not indirect:
        summary = "The selected files have no import links within depth 3."
    return {"summary": summary, "rows": rows[:12]}


def _propagation(blast: list[dict]) -> dict:
    _risk, parts = blast_risk(blast)
    by_module = {item["module"]: item for item in parts}
    downstream = [item for item in blast if item["depth"] > 0]
    downstream.sort(key=lambda item: (-by_module[item["module"]]["pressure"], item["depth"], item["module"]))
    rows = []
    for item in downstream[:8]:
        part = by_module[item["module"]]
        rows.append(
            {
                "title": item["module"],
                "detail": (
                    f"depth {item['depth']} · exposure {part['exposure']:.2f} · "
                    f"local pressure {part['pressure']:.2f}"
                ),
            }
        )
    if not downstream:
        summary = "No importer sits downstream of the edit."
    else:
        lead = rows[0]
        summary = (
            f"{len(downstream)} downstream modules. "
            f"The largest local pressure is {by_module[lead['title']]['pressure']:.2f} on {lead['title']}."
        )
    return {"summary": summary, "rows": rows}


def _semantic(modules, symbols, commits, hops) -> dict:
    rows = []
    sentences = []
    for path in modules[:2]:
        parsed = symbols.get(path) or _empty_symbols()
        names = [item["name"] for item in parsed["api"][:3]]
        extra = len(parsed["api"]) - len(names)
        if names:
            listed = ", ".join(names) + f", and {extra} more" if extra > 0 else _join(names)
            sentence = f"{path} defines {listed}."
        elif parsed["classes"]:
            sentence = f"{path} defines the class {parsed['classes'][0]['name']}."
        else:
            sentence = f"{path} has no parsed function or class in the file text."
        callers = [item["module"] for item in hops["direct_importers"] if item.get("origin") == path]
        if callers:
            shown = callers[:3]
            extra_callers = len(callers) - len(shown)
            who = ", ".join(shown) + f", and {extra_callers} more" if extra_callers else _join(shown)
            verb = "imports" if len(callers) == 1 else "import"
            sentence += f" {who} {verb} it."
        commit = commits.get(path)
        if commit:
            sentence += f" The latest recorded commit says “{commit['summary']}” ({_ago(commit['age'])})."
        routes = [item["path"] for item in parsed["routes"][:3]]
        if routes:
            extra_routes = len(parsed["routes"]) - len(routes)
            listed_routes = ", ".join(routes) + f", and {extra_routes} more" if extra_routes else _join(routes)
            sentence += f" It calls {listed_routes}."
        sentences.append(sentence)
        if names:
            rows.append({"title": names[0], "detail": f"defined in {path}"})
        if commit:
            rows.append({"title": commit["summary"], "detail": f"{_ago(commit['age'])} · {commit['author']} · {path}"})
        for route in parsed["routes"][:3]:
            rows.append({"title": route["path"], "detail": f"called from line {route['line']} · {path}"})
    if len(modules) > 2:
        sentences.append(f"{len(modules) - 2} more selected files are in the proposal.")
    if not sentences:
        sentences.append("No selected file could be read.")
    return {"summary": " ".join(sentences), "rows": rows[:8]}


def _test_engine(analysis: dict) -> dict:
    rows = []
    for item in analysis.get("tests") or []:
        place = "in the tree" if item.get("in_tree") else "named in history, file not in the tree"
        rows.append({"title": item["path"], "detail": f"{item.get('reason') or 'test'} · {place}"})
    for path in analysis.get("uncovered") or []:
        rows.append({"title": path, "detail": "no test covers this file"})
    if rows:
        named = [row["title"] for row in rows if "no test covers" not in row["detail"]]
        summary = "Run " + _join(named) + "." if named else "No named test is tied to this change."
        missing = [row["title"] for row in rows if row["detail"] == "no test covers this file"]
        if missing:
            summary += f" No test covers {_join(missing)}."
    else:
        summary = "No covering test and no recorded test name is tied to this change."
    return {"summary": summary, "rows": rows[:8]}


def _risk_engine(report: dict, by_path: dict) -> dict:
    risk, parts = blast_risk(report["blast"])
    delivery = report["delay_pressure"]
    operational = 0.0
    operational_module = ""
    if report["blast"]:
        lead = max(
            report["blast"],
            key=lambda item: (by_path[item["module"]]["risks"]["incident"], item["module"]),
        )
        operational = round(float(by_path[lead["module"]]["risks"]["incident"]), 4)
        operational_module = lead["module"]
    delay_module = ""
    edited = [item for item in report["blast"] if item["depth"] == 0]
    if edited:
        delay_module = max(edited, key=lambda item: item["delay"])["module"]
    top = sorted(parts, key=lambda item: (-item["pressure"], item["depth"], item["module"]))[:3]
    rows = [
        {"title": "Change risk index", "detail": f"{risk:.2f} · blast of {len(report['blast'])} modules"},
        {"title": "Delivery delay index", "detail": f"{delivery:.2f} on {delay_module}"},
        {"title": "Incident index", "detail": f"{operational:.2f} on {operational_module}"},
    ]
    for item in top:
        rows.append(
            {
                "title": item["module"],
                "detail": f"local pressure {item['pressure']:.2f} · depth {item['depth']}",
            }
        )
    return {
        "summary": (
            f"The change risk index is {risk:.2f}. "
            f"Delivery delay on the edited files is {delivery:.2f}. "
            f"The highest incident index in the blast is {operational:.2f}. "
            "These are the indexes already on the twin."
        ),
        "change_risk": risk,
        "delivery": delivery,
        "operational": operational,
        "rows": rows,
    }


def _explanation(blast: list[dict], hops: dict) -> dict:
    via = {item["module"]: item for item in hops["direct_importers"] + hops["indirect_importers"]}
    rows = []
    for item in blast:
        if item["depth"] == 0:
            detail = "Selected in the proposal."
        else:
            hop = via.get(item["module"]) or {}
            chain = hop.get("chain") or [item["module"]]
            if len(chain) >= 2:
                detail = f"{chain[-1]} imports {chain[-2]}."
                if len(chain) > 2:
                    detail = f"{detail} That reaches {chain[0]} in {item['depth']} hops."
            else:
                detail = f"Reached at depth {item['depth']}."
        rows.append({"title": item["module"], "detail": detail})
    count = len(rows)
    summary = f"{count} component{'s' if count != 1 else ''} explained from the import walk."
    return {"summary": summary, "rows": rows[:12]}


def _provenance(modules, symbols, commits, hops, report, risk) -> dict:
    rows = []
    for path in modules:
        rows.append({"title": f"File {path}", "detail": "selected file text", "kind": "file"})
        parsed = symbols.get(path) or _empty_symbols()
        for item in (parsed["api"] or parsed["classes"])[:2]:
            rows.append(
                {
                    "title": f"{item['name']} at {path}:{item['line']}",
                    "detail": item.get("kind") or "class",
                    "kind": "symbol",
                }
            )
        commit = commits.get(path)
        if commit:
            rows.append(
                {
                    "title": commit["summary"],
                    "detail": f"commit {_ago(commit['age'])} · {commit['author']} · {path}",
                    "kind": "history",
                }
            )
    for item in hops["direct_importers"][:4]:
        rows.append(
            {
                "title": f"{item['module']} imports {item['via']}",
                "detail": "import edge",
                "kind": "edge",
            }
        )
    for item in (report.get("analysis") or {}).get("tests") or []:
        rows.append(
            {
                "title": item["path"],
                "detail": item.get("reason") or "test",
                "kind": "test",
            }
        )
    for path_row in (report.get("impact") or {}).get("paths") or []:
        if path_row.get("kind") not in {"package", "failure", "plugin"}:
            continue
        rows.append(
            {
                "title": f"{path_row['kind']} {path_row.get('object') or ''}".strip(),
                "detail": path_row.get("summary") or path_row.get("via") or "",
                "kind": "fact",
            }
        )
        if len([row for row in rows if row["kind"] == "fact"]) >= 3:
            break
    rows.append(
        {
            "title": f"Change risk index {risk['change_risk']:.2f}",
            "detail": "blast-radius index from bug, test gap, and depth",
            "kind": "score",
        }
    )
    return {
        "summary": f"{len(rows)} citations back to a file, symbol, edge, test, fact, or the change risk index.",
        "rows": rows[:16],
    }


def _hops(view: dict, modules: list[str]) -> dict:
    by_path = {module["path"]: module for module in view["modules"]}
    reverse = adjacency(view.get("edges") or [], reverse=True)
    forward = adjacency(view.get("edges") or [], reverse=False)
    importer_depth, importer_parent = _walk(modules, reverse)
    dependency_depth, dependency_parent = _walk(modules, forward)
    return {
        "direct_importers": _hop_rows(importer_depth, importer_parent, modules, by_path, direct=True),
        "indirect_importers": _hop_rows(importer_depth, importer_parent, modules, by_path, direct=False),
        "direct_dependencies": _hop_rows(dependency_depth, dependency_parent, modules, by_path, direct=True),
        "indirect_dependencies": _hop_rows(dependency_depth, dependency_parent, modules, by_path, direct=False),
    }


def _walk(starts: list[str], graph: dict[str, list[str]]) -> tuple[dict[str, int], dict[str, str | None]]:
    dist = {node: 0 for node in starts}
    parent: dict[str, str | None] = {node: None for node in starts}
    queue: deque[str] = deque(starts)
    while queue:
        node = queue.popleft()
        if dist[node] >= 3:
            continue
        for nxt in graph.get(node, []):
            if nxt not in dist:
                dist[nxt] = dist[node] + 1
                parent[nxt] = node
                queue.append(nxt)
    return dist, parent


def _hop_rows(dist, parent, starts, by_path, direct: bool) -> list[dict]:
    rows = []
    wanted = 1 if direct else None
    for path, depth in sorted(dist.items(), key=lambda item: (item[1], item[0])):
        if depth == 0:
            continue
        if wanted is not None and depth != wanted:
            continue
        if wanted is None and depth < 2:
            continue
        if path not in by_path:
            continue
        chain = _chain(path, parent, set(starts))
        via = chain[-2] if len(chain) >= 2 else ""
        origin = chain[0] if chain else ""
        rows.append(
            {
                "module": path,
                "service": by_path[path]["service"],
                "depth": depth,
                "via": via,
                "origin": origin,
                "chain": chain,
            }
        )
    return rows


def _chain(path: str, parent: dict[str, str | None], starts: set[str]) -> list[str]:
    chain = [path]
    seen = set()
    while chain[-1] not in starts and parent.get(chain[-1]) and chain[-1] not in seen:
        seen.add(chain[-1])
        chain.append(parent[chain[-1]])
    chain.reverse()
    return chain


def _commits(facts: list[dict], modules: list[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    selected = set(modules)
    for fact in facts:
        if fact.get("predicate") != "changed" or fact.get("object") not in selected:
            continue
        age = float(fact.get("at_age") or 0)
        current = found.get(fact["object"])
        if current is not None and current["age"] <= age:
            continue
        found[fact["object"]] = {
            "age": age,
            "author": str(fact.get("subject") or ""),
            "summary": str(fact.get("summary") or "commit"),
        }
    return found


def _symbols(text: str, language: str) -> dict:
    if not text:
        return _empty_symbols()
    if language == "python":
        return _python_symbols(text)
    if language in {"typescript", "javascript"}:
        return _script_symbols(text)
    return _empty_symbols()


def _python_symbols(text: str) -> dict:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _empty_symbols()
    functions = []
    classes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({"name": node.name, "line": node.lineno, "kind": "function"})
        elif isinstance(node, ast.ClassDef):
            classes.append({"name": node.name, "line": node.lineno, "kind": "class"})
    api = [item for item in functions if not item["name"].startswith("_")]
    return {"functions": functions, "classes": classes, "api": api, "routes": _routes(text)}


def _script_symbols(text: str) -> dict:
    functions = []
    classes = []
    seen = set()
    for pattern, kind, bucket in (
        (FUNC_RE, "function", functions),
        (CONST_FN, "function", functions),
        (CLASS_RE, "class", classes),
    ):
        for match in pattern.finditer(text):
            name = match.group(1)
            if name in seen:
                continue
            seen.add(name)
            line = text[: match.start()].count("\n") + 1
            bucket.append({"name": name, "line": line, "kind": kind})
    exported = set()
    for match in EXPORT_BLOCK.finditer(text):
        for part in match.group(1).split(","):
            piece = part.strip()
            if not piece:
                continue
            name = piece.split(" as ")[0].strip().split()[-1]
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                exported.add(name)
    api = [item for item in functions if item["name"] in exported] if exported else [
        item for item in functions if not item["name"].startswith("_")
    ]
    return {"functions": functions, "classes": classes, "api": api, "routes": _routes(text)}


def _routes(text: str) -> list[dict]:
    found = []
    seen = set()
    for match in ROUTE_RE.finditer(text):
        path = match.group(1)
        if path in seen:
            continue
        seen.add(path)
        found.append({"path": path, "line": text[: match.start()].count("\n") + 1})
    return found


def _empty_symbols() -> dict:
    return {"functions": [], "classes": [], "api": [], "routes": []}


def _read_source(view: dict, rel: str) -> str:
    root = str((view.get("project") or {}).get("root") or "")
    if not root or not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return ""
    base = Path(root).resolve()
    path = (base / rel).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return ""
    if not path.is_file():
        return ""
    data = path.read_bytes()[:MAX_FILE_BYTES]
    if b"\0" in data[:1024]:
        return ""
    return data.decode("utf-8", errors="replace")


def _ago(age: float) -> str:
    number = float(age or 0)
    if abs(number - round(number)) < 1e-6:
        shown = str(int(round(number)))
    else:
        shown = f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{shown}d ago"


def _join(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]
