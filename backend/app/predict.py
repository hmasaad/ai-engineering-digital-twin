"""Seven engineering questions, answered from history and the indexes already scored.

The bug, incident, debt, and dependency indexes are read, not refit. A band
widens when fewer than three outcomes are recorded. A recorded build failure
is evidence for a dependency forecast, not confirmation that the forecast
predicted it. None of these figures is a calibrated probability.
"""

from __future__ import annotations

from app.temporal import holds

QUESTIONS = (
    ("bugs", "Which modules are likely to introduce bugs?"),
    ("pull_requests", "Which pull requests have a high chance of regression?"),
    ("builds", "Which builds are likely to fail?"),
    ("dependencies", "Which dependencies are becoming unstable?"),
    ("releases", "Which releases have elevated failure risk?"),
    ("incidents", "Which components are likely to generate production incidents?"),
    ("debt", "When is technical debt likely to become an operational problem?"),
)


def answer(view: dict) -> dict:
    questions = [_question(key, text, view) for key, text in QUESTIONS]
    report = {
        "questions": questions,
        "summary": _summary(questions),
        "note": (
            "These answers read the bug, incident, debt, and dependency indexes already on the twin, "
            "together with recorded pull requests, builds, releases, and incidents. "
            "They do not replace those indexes. "
            "A band widens when fewer than three outcomes are recorded. "
            "A recorded build failure is evidence for a dependency forecast, not confirmation that the forecast predicted it. "
            "An index is not a calibrated probability. "
            "Twelve algorithms then read these answers in order. The defect index stays the bug index. "
            "A message pass reads the import graph and is not a trained network. Weights are not retrained."
        ),
    }
    from app.predict_algorithms import run_algorithms

    report["algorithms"] = run_algorithms(view, report)
    return report


def _question(key: str, text: str, view: dict) -> dict:
    rows = {
        "bugs": _bugs,
        "pull_requests": _pull_requests,
        "builds": _builds,
        "dependencies": _dependencies,
        "releases": _releases,
        "incidents": _incidents,
        "debt": _debt,
    }[key](view)
    return {"id": key, "question": text, "summary": _answer(key, rows), "rows": rows[:5]}


def _bugs(view: dict) -> list[dict]:
    rows = []
    for module in _code(view):
        score = float(module["risks"]["bug"])
        if score < 0.35:
            continue
        low, high = _band(score, view)
        rows.append(
            {
                "title": module["path"],
                "index": round(score, 4),
                "low": low,
                "high": high,
                "detail": f"Bug index {score:.4f}. The figure is the module's existing bug index.",
            }
        )
    rows.sort(key=lambda item: (-item["index"], item["title"]))
    return rows


def _pull_requests(view: dict) -> list[dict]:
    facts = _facts(view)
    by_path = {module["path"]: module for module in _code(view)}
    known = set(by_path)
    grouped: dict[str, dict] = {}
    for fact in facts:
        if fact.get("predicate") != "modifies" or not str(fact.get("subject") or "").startswith("pr:"):
            continue
        bucket = grouped.setdefault(
            str(fact["subject"]),
            {"files": [], "tests": [], "summary": fact.get("summary") or "", "age": fact.get("from_age")},
        )
        path = str(fact.get("object") or "")
        if path and path not in bucket["files"]:
            bucket["files"].append(path)
    for fact in facts:
        if fact.get("predicate") != "affects" or str(fact.get("subject") or "") not in grouped:
            continue
        path = str(fact.get("object") or "")
        if path and path not in grouped[str(fact["subject"])]["tests"]:
            grouped[str(fact["subject"])]["tests"].append(path)
    rows = []
    for subject, item in grouped.items():
        scores = [float(by_path[path]["risks"]["bug"]) for path in item["files"] if path in by_path]
        if not scores:
            continue
        score = max(scores)
        missing = [path for path in item["tests"] if path not in known]
        low, high = _band(score, view)
        number = subject.split(":", 1)[1]
        guard = (
            f" The named test {missing[0]} is not in the tree."
            if missing
            else " A named test is in the tree."
            if item["tests"]
            else " No test is named on the pull request."
        )
        lead = max((path for path in item["files"] if path in by_path), key=lambda path: float(by_path[path]["risks"]["bug"]))
        rows.append(
            {
                "title": f"Pull request {number}",
                "index": round(score, 4),
                "low": low,
                "high": high,
                "detail": (
                    f"It modifies {lead}, whose bug index is {score:.4f}.{guard} "
                    f"{item['summary']}".strip()
                ),
            }
        )
    rows.sort(key=lambda item: (-item["index"], item["title"]))
    return rows


def _builds(view: dict) -> list[dict]:
    failures = (view.get("engineering_failures") or {}).get("failures") or []
    grouped: dict[str, dict] = {}
    for row in failures:
        if row.get("mode") != "build":
            continue
        current = grouped.get(row["package"])
        if current is None or float(row["score"]) > float(current["score"]):
            grouped[row["package"]] = row
    recorded = []
    for fact in _facts(view):
        if fact.get("predicate") != "failed" or fact.get("kind") != "event":
            continue
        recorded.append(str(fact.get("subject") or "build"))
    rows = []
    for name, row in grouped.items():
        evidence = f" Recorded {', '.join(recorded)} is the evidence for this forecast, not a confirmation of it." if recorded else ""
        rows.append(
            {
                "title": name,
                "index": round(float(row["score"]), 4),
                "low": row["low"],
                "high": row["high"],
                "detail": (
                    f"A build failure is forecast through {name}, landing on {row['module']} "
                    f"at {float(row['score']):.4f}.{evidence}"
                ),
            }
        )
    rows.sort(key=lambda item: (-item["index"], item["title"]))
    return rows


def _dependencies(view: dict) -> list[dict]:
    packages = {item["name"]: item for item in (view.get("dependencies") or {}).get("packages") or []}
    upgrades: dict[str, float] = {}
    failures: dict[str, float] = {}
    for fact in _facts(view):
        if fact.get("predicate") == "depends_on" and holds(fact, 0):
            name = _pkg_name(str(fact.get("object") or ""))
            opened = float(fact.get("from_age") or 0)
            if 0 < opened <= 90:
                upgrades[name] = min(upgrades.get(name, opened), opened)
        if fact.get("predicate") == "attributed_to" and fact.get("kind") == "event":
            name = _pkg_name(str(fact.get("object") or ""))
            age = fact.get("at_age")
            if name and age is not None:
                failures[name] = min(failures.get(name, float(age)), float(age))
    rows = []
    for name, upgraded in upgrades.items():
        failed = failures.get(name)
        if failed is None or failed >= upgraded:
            continue
        package = packages.get(name)
        if package is None:
            continue
        score = float(package["risk"])
        low, high = _band(score, view)
        rows.append(
            {
                "title": name,
                "index": round(score, 4),
                "low": low,
                "high": high,
                "detail": (
                    f"Upgraded {_age(upgraded)}d ago, then a build failed {_age(failed)}d ago. "
                    f"The dependency index is {score:.4f}."
                ),
            }
        )
    rows.sort(key=lambda item: (-item["index"], item["title"]))
    return rows


def _releases(view: dict) -> list[dict]:
    incident_index = {}
    for module in _code(view):
        service = module["service"]
        score = float(module["risks"]["incident"])
        incident_index[service] = max(incident_index.get(service, 0.0), score)
    incidents = []
    deploys = []
    for fact in _facts(view):
        if fact.get("predicate") == "incident_on" and fact.get("kind") == "event":
            incidents.append(fact)
        if fact.get("predicate") == "deployed" and fact.get("kind") == "event":
            deploys.append(fact)
    rows = []
    for fact in deploys:
        service = str(fact.get("subject") or "")
        result = str(fact.get("object") or "")
        age = float(fact.get("at_age") or 0)
        score = incident_index.get(service, 0.0)
        low, high = _band(score, view)
        followed = [
            item
            for item in incidents
            if str(item.get("object") or "") == service
            and item.get("at_age") is not None
            and float(item["at_age"]) <= age
            and age - float(item["at_age"]) <= 20
        ]
        extra = ""
        if followed:
            extra = f" An incident followed: {followed[0].get('summary') or followed[0].get('object')}."
        observed = "The release rolled back." if result == "rollback" else "The deploy succeeded."
        rows.append(
            {
                "title": f"{service} · {_age(age)}d ago · {result}",
                "index": round(score, 4),
                "low": low,
                "high": high,
                "observed": result,
                "detail": (
                    f"{observed} {fact.get('summary') or ''}. "
                    f"The {service} incident index is {score:.4f}.{extra}"
                ).strip(),
            }
        )
    rows.sort(key=lambda item: (item.get("observed") != "rollback", -item["index"], item["title"]))
    return rows


def _incidents(view: dict) -> list[dict]:
    latest = {}
    for fact in _facts(view):
        if fact.get("predicate") != "incident_on" or fact.get("kind") != "event":
            continue
        service = str(fact.get("object") or "")
        age = float(fact.get("at_age") or 0)
        current = latest.get(service)
        if current is None or age < current["age"]:
            latest[service] = {"age": age, "summary": fact.get("summary") or service}
    rows = []
    for module in _code(view):
        score = float(module["risks"]["incident"])
        if score < 0.35:
            continue
        low, high = _band(score, view)
        history = latest.get(module["service"])
        recorded = f" Recorded on {module['service']}: {history['summary']}." if history else ""
        horizon = _horizon(view, module["path"])
        rows.append(
            {
                "title": module["path"],
                "index": round(score, 4),
                "low": low,
                "high": high,
                "detail": f"Incident index {score:.4f}, {horizon}.{recorded}",
            }
        )
    rows.sort(key=lambda item: (-item["index"], item["title"]))
    return rows


def _debt(view: dict) -> list[dict]:
    rows = []
    for module in _code(view):
        debt = float(module.get("debt") or 0)
        incident = float(module["risks"]["incident"])
        if debt < 0.35 or incident < 0.35:
            continue
        low, high = _band(debt, view)
        horizon = _horizon(view, module["path"])
        rows.append(
            {
                "title": module["path"],
                "index": round(debt, 4),
                "low": low,
                "high": high,
                "detail": (
                    f"Debt {debt:.4f} sits with incident index {incident:.4f}, {horizon}."
                ),
            }
        )
    rows.sort(key=lambda item: (-item["index"], item["title"]))
    return rows


def _horizon(view: dict, path: str) -> str:
    row = ((view.get("forecast") or {}).get("by_module") or {}).get(path) or {}
    return row.get("horizon") or "later in the history window"


def _answer(key: str, rows: list[dict]) -> str:
    if not rows:
        empty = {
            "bugs": "No module has a bug index at or above 0.35.",
            "pull_requests": "No recorded pull request modifies a module in the tree.",
            "builds": "No build failure is forecast from the current dependencies.",
            "dependencies": "No dependency was upgraded and then failed more recently.",
            "releases": "No release is recorded.",
            "incidents": "No module has an incident index at or above 0.35.",
            "debt": "No module pairs debt at or above 0.35 with an incident index at or above 0.35.",
        }
        return empty[key]
    lead = rows[0]
    if key == "bugs":
        return f"{lead['title']} leads the bug index at {lead['index']:.2f}."
    if key == "pull_requests":
        return f"{lead['title']} carries the highest bug index among the files it modifies, {lead['index']:.2f}."
    if key == "builds":
        return f"{lead['title']} carries the build forecast at {lead['index']:.2f}."
    if key == "dependencies":
        return f"{lead['title']} is becoming unstable. Its dependency index is {lead['index']:.2f}."
    if key == "releases":
        return f"{lead['title']} is the release to read first. The service incident index is {lead['index']:.2f}."
    if key == "incidents":
        return f"{lead['title']} leads the incident index at {lead['index']:.2f}."
    return f"{lead['title']} pairs debt {lead['index']:.2f} with an incident index already on that file."


def _summary(questions: list[dict]) -> str:
    by_id = {item["id"]: item for item in questions}
    parts = []
    bugs = (by_id["bugs"]["rows"] or [None])[0]
    if bugs:
        parts.append(f"{bugs['title']} leads the bug index at {bugs['index']:.2f}")
    pulls = (by_id["pull_requests"]["rows"] or [None])[0]
    if pulls:
        parts.append(f"{pulls['title']} is the sharpest recorded pull request")
    deps = (by_id["dependencies"]["rows"] or [None])[0]
    if deps:
        parts.append(f"{deps['title']} is becoming unstable at {deps['index']:.2f}")
    releases = [row for row in by_id["releases"]["rows"] if row.get("observed") == "rollback"]
    if releases:
        parts.append(f"{releases[0]['title']} rolled back")
    if not parts:
        return "Recorded history does not yet name a module, pull request, dependency, or release."
    if len(parts) == 1:
        return parts[0] + "."
    return ", ".join(parts[:-1]) + ", and " + parts[-1] + "."


def _band(score: float, view: dict) -> tuple[float, float]:
    width = 0.12 if len(view.get("outcomes") or []) < 3 else 0.08
    return round(max(0.0, score - width), 4), round(min(1.0, score + width), 4)


def _code(view: dict) -> list[dict]:
    return [module for module in view.get("modules") or [] if module.get("role") == "code"]


def _facts(view: dict) -> list[dict]:
    return ((view.get("dynamics") or {}).get("facts")) or []


def _pkg_name(label: str) -> str:
    if label.startswith("@") and label.count("@") >= 2:
        return label.rsplit("@", 1)[0]
    if "@" in label and not label.startswith("@"):
        return label.split("@", 1)[0]
    return label


def _age(value: float) -> str:
    number = float(value)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")
