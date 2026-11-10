"""Twelve failure-prediction steps, each reading the step before it.

Features, history, and the import graph are read from the twin. The defect
index stays the bug index already scored. A message pass is a fixed readout
of neighboring bug indexes, not a trained network. Weights are not retrained.
A recorded build failure does not count as confirmation. An index is not a
calibrated probability.
"""

from __future__ import annotations

WINDOW_DAYS = 20.0
TITLES = (
    "Engineering Failure Feature Engineering",
    "Temporal and Sequential Failure Modeling",
    "Software Defect Prediction Models",
    "Pull Request Regression Prediction",
    "Build Failure Prediction",
    "Graph Neural Networks for Dependency-Aware Failure Prediction",
    "Survival Analysis and Time-to-Failure Prediction",
    "Bayesian Failure Probability Estimation",
    "Uncertainty Quantification and Prediction Calibration",
    "Explainable AI for Engineering Failure Predictions",
    "Concept Drift Detection and Model Retraining",
    "Online Prediction Evaluation",
)


def run_algorithms(view: dict, report: dict) -> dict:
    features = _features(view, report)
    temporal = _temporal(view, features)
    defect = _defect(view, features)
    pull = _pull(report)
    build = _build(report)
    graph = _graph(view, defect)
    survival = _survival(view, defect)
    bayes = _bayes(view, defect)
    uncertainty = _uncertainty(view, bayes)
    explanation = _explain(features, temporal, defect, pull, build, graph, survival)
    drift = _drift(view, temporal, uncertainty)
    evaluation = _evaluate(view, report, drift)
    steps = [features, temporal, defect, pull, build, graph, survival, bayes, uncertainty, explanation, drift, evaluation]
    return {
        "steps": steps,
        "note": (
            "The steps run in order. Features and the commit sequence feed the reading. "
            "The defect index stays the bug index already scored. "
            "A pull request and a build keep the indexes from the questions above. "
            "One message pass reads neighboring bug indexes. It is not a trained network, and it does not replace the bug index. "
            "The days since a recorded incident are observed, not a fitted lifetime. "
            "With no recorded outcome the posterior stays the prior. "
            "Weights are not retrained. A recorded build failure is evidence, not confirmation. "
            "An index is not a calibrated probability."
        ),
    }


def _step(order: int, title: str, summary: str, rows: list[dict], **extra) -> dict:
    return {"order": order, "title": title, "summary": summary, "rows": rows, **extra}


def _lead(report: dict, view: dict) -> dict | None:
    rows = _question(report, "bugs")
    if rows:
        path = rows[0]["title"]
        module = next((item for item in _code(view) if item["path"] == path), None)
        if module is not None:
            return module
    code = _code(view)
    if not code:
        return None
    return max(code, key=lambda item: (float(item["risks"]["bug"]), item["path"]))


def _features(view: dict, report: dict) -> dict:
    module = _lead(report, view)
    if module is None:
        return _step(1, TITLES[0], "No module is available to read features from.", [], module=None, values={})
    values = {
        "test_gap": round(float(module.get("test_gap") or 0), 4),
        "churn": round(float(module.get("churn") or 0), 2),
        "complexity": int(module.get("complexity") or 0),
        "authors": len(module.get("authors") or []),
        "debt": round(float(module.get("debt") or 0), 4),
        "incident": round(float(module["risks"]["incident"]), 4),
    }
    rows = [
        {"title": "test gap", "detail": f"{values['test_gap']:.2f} · share of the file without a linked test"},
        {"title": "churn", "detail": f"{values['churn']:.2f} decayed lines"},
        {"title": "complexity", "detail": str(values["complexity"])},
        {"title": "authors", "detail": str(values["authors"])},
        {"title": "debt", "detail": f"{values['debt']:.4f}"},
        {"title": "incident index", "detail": f"{values['incident']:.4f}"},
    ]
    summary = (
        f"Features for {module['path']} are the test gap, churn, complexity, authors, debt, and incident index already on the file. "
        "The bug index is the label, not one of these inputs."
    )
    return _step(1, TITLES[0], summary, rows, module=module["path"], service=module["service"], values=values)


def _temporal(view: dict, features: dict) -> dict:
    path = features.get("module")
    if not path:
        return _step(2, TITLES[1], "No file has a history sequence.", [], events=[])
    events = []
    for fact in _facts(view):
        if fact.get("predicate") == "changed" and fact.get("object") == path and fact.get("at_age") is not None:
            events.append(
                {
                    "kind": "change",
                    "age": float(fact["at_age"]),
                    "label": f"{fact.get('subject') or 'someone'} changed {path}",
                }
            )
    service = features.get("service") or ""
    for fact in _facts(view):
        if fact.get("predicate") == "incident_on" and fact.get("object") == service and fact.get("at_age") is not None:
            events.append(
                {
                    "kind": "incident",
                    "age": float(fact["at_age"]),
                    "label": fact.get("summary") or f"{service} incident",
                }
            )
    events.sort(key=lambda item: item["age"])
    recent = [item for item in events if item["kind"] == "change" and item["age"] < WINDOW_DAYS]
    rows = [
        {"title": f"{_age(item['age'])}d ago", "detail": f"{item['kind']} · {item['label']}"}
        for item in events[:6]
    ]
    if events:
        newest = events[0]
        summary = (
            f"{len(events)} recorded events touch {path} or its service, newest {_age(newest['age'])}d ago. "
            f"{len(recent)} changes fall inside {int(WINDOW_DAYS)} days."
        )
    else:
        summary = f"No commit or incident is recorded for {path}."
    return _step(2, TITLES[1], summary, rows, events=events, recent=len(recent))


def _defect(view: dict, features: dict) -> dict:
    module = _module(view, features.get("module"))
    if module is None:
        return _step(3, TITLES[2], "No module has a bug index to read.", [], point=None, module=None)
    point = round(float(module["risks"]["bug"]), 4)
    summary = f"The defect index for {module['path']} stays the bug index {point:.4f}. The weights are not refit."
    return _step(
        3,
        TITLES[2],
        summary,
        [{"title": module["path"], "detail": f"bug index {point:.4f}"}],
        point=point,
        module=module["path"],
    )


def _pull(report: dict) -> dict:
    rows = _question(report, "pull_requests")
    if not rows:
        return _step(
            4,
            TITLES[3],
            "No recorded pull request modifies a module in the tree. Nothing is added to the defect index.",
            [],
            point=None,
        )
    lead = rows[0]
    summary = (
        f"{lead['title']} keeps the bug index of the file it modifies, {float(lead['index']):.4f}. "
        "That index is not refit."
    )
    return _step(
        4,
        TITLES[3],
        summary,
        [{"title": lead["title"], "detail": lead["detail"]}],
        point=float(lead["index"]),
        title_text=lead["title"],
    )


def _build(report: dict) -> dict:
    rows = _question(report, "builds")
    if not rows:
        return _step(
            5,
            TITLES[4],
            "No build failure is forecast from the current dependencies.",
            [],
            point=None,
        )
    lead = rows[0]
    summary = (
        f"{lead['title']} keeps the dependency forecast at {float(lead['index']):.4f}. "
        "The recorded failure is the evidence for that forecast, not a confirmation of it."
    )
    return _step(
        5,
        TITLES[4],
        summary,
        [{"title": lead["title"], "detail": lead["detail"]}],
        point=float(lead["index"]),
        package=lead["title"],
    )


def _graph(view: dict, defect: dict) -> dict:
    path = defect.get("module")
    module = _module(view, path) if path else None
    if module is None:
        return _step(6, TITLES[5], "No import graph is read. The bug index stays where it is.", [], point=None)
    own = float(module["risks"]["bug"])
    neighbors = []
    for edge in view.get("edges") or []:
        other = ""
        direction = ""
        if edge.get("source") == path:
            other = edge.get("target") or ""
            direction = "imports"
        elif edge.get("target") == path:
            other = edge.get("source") or ""
            direction = "imported by"
        if not other or any(item["module"] == other for item in neighbors):
            continue
        neighbor = _module(view, other)
        if neighbor is None:
            continue
        neighbors.append(
            {
                "module": other,
                "direction": direction,
                "bug": round(float(neighbor["risks"]["bug"]), 4),
            }
        )
    neighbor_mean = round(sum(item["bug"] for item in neighbors) / len(neighbors), 4) if neighbors else None
    rows = [
        {
            "title": f"{path} {item['direction']} {item['module']}",
            "detail": f"neighbor bug index {item['bug']:.4f}",
        }
        for item in sorted(neighbors, key=lambda item: (-item["bug"], item["module"]))[:6]
    ]
    if neighbor_mean is None:
        context = "The file has no scored neighbor."
    else:
        context = f"The neighbor mean is {neighbor_mean:.4f}."
    summary = (
        f"One message pass reads the import graph around {path}. The bug index stays {own:.4f}. {context} "
        "This is a fixed readout, not a trained network, and it does not replace the bug index."
    )
    return _step(
        6,
        TITLES[5],
        summary,
        rows,
        point=round(own, 4),
        neighbor_mean=neighbor_mean,
        neighbors=neighbors,
        module=path,
    )


def _survival(view: dict, defect: dict) -> dict:
    path = defect.get("module")
    module = _module(view, path) if path else None
    if module is None:
        return _step(7, TITLES[6], "No service has a recorded failure time.", [], days=None)
    service = module["service"]
    ages = [
        float(fact["at_age"])
        for fact in _facts(view)
        if fact.get("predicate") == "incident_on"
        and fact.get("object") == service
        and fact.get("at_age") is not None
    ]
    if not ages:
        summary = (
            f"No incident is recorded on {service}. No time-to-failure is fit. "
            "The bug index is not a lifetime."
        )
        return _step(7, TITLES[6], summary, [], days=None, service=service)
    days = min(ages)
    summary = (
        f"{_age(days)} days since the latest recorded {service} incident. "
        "That gap is observed. No survival curve is fit, and the bug index is not a lifetime."
    )
    return _step(
        7,
        TITLES[6],
        summary,
        [{"title": service, "detail": f"latest incident {_age(days)}d ago"}],
        days=days,
        service=service,
    )


def _bayes(view: dict, defect: dict) -> dict:
    path = defect.get("module")
    module = _module(view, path) if path else None
    if module is None:
        return _step(8, TITLES[7], "No prior index is available.", [], prior=None, posterior=None)
    prior = round(float(module["risks"]["bug"]), 4)
    outcomes = [item for item in view.get("outcomes") or [] if item.get("module") == path]
    if not outcomes:
        posterior = prior
        summary = (
            f"The prior is the bug index {prior:.4f}. No recorded outcome updates it, so the posterior stays {posterior:.4f}. "
            "The figure is not written back, and it is not a calibrated probability."
        )
    else:
        happened = sum(1.0 if item.get("happened") else 0.0 for item in outcomes)
        posterior = round((3.0 * prior + happened) / (3.0 + len(outcomes)), 4)
        summary = (
            f"The prior {prior:.4f} and {len(outcomes)} outcomes give a posterior {posterior:.4f}. "
            "The figure is not written onto the bug index, and it is not a calibrated probability."
        )
    return _step(
        8,
        TITLES[7],
        summary,
        [{"title": path or "", "detail": f"prior {prior:.4f} · posterior {posterior:.4f} · outcomes {len(outcomes)}"}],
        prior=prior,
        posterior=posterior,
        outcomes=len(outcomes),
        module=path,
    )


def _uncertainty(view: dict, bayes: dict) -> dict:
    point = bayes.get("posterior")
    if point is None:
        return _step(
            9,
            TITLES[8],
            "No posterior is available to bound.",
            [],
            reading="unvalidated",
            point=None,
            low=None,
            high=None,
            width=None,
        )
    outcomes_n = len(view.get("outcomes") or [])
    width = 0.12 if outcomes_n < 3 else 0.08
    low = round(max(0.0, point - width), 4)
    high = round(min(1.0, point + width), 4)
    reading = "uncalibrated" if outcomes_n < 3 else "compared"
    if reading == "uncalibrated":
        summary = (
            f"The band around the posterior {point:.4f} is {low:.2f}–{high:.2f}. "
            "Fewer than 3 outcomes are recorded, so the index is uncalibrated. The bug index is not moved."
        )
    else:
        summary = (
            f"The band around the posterior {point:.4f} is {low:.2f}–{high:.2f}. "
            "The comparison does not move the bug index."
        )
    return _step(
        9,
        TITLES[8],
        summary,
        [{"title": bayes.get("module") or "", "detail": f"index {point:.4f} · band {low:.2f}–{high:.2f} · width {width:.2f} · {reading}"}],
        reading=reading,
        point=point,
        low=low,
        high=high,
        width=width,
    )


def _explain(features, temporal, defect, pull, build, graph, survival) -> dict:
    parts = []
    if features.get("module"):
        values = features.get("values") or {}
        parts.append(
            f"{features['module']} has test gap {values.get('test_gap', 0):.2f} and churn {values.get('churn', 0):.0f}"
        )
    changes = [item for item in temporal.get("events") or [] if item["kind"] == "change"]
    if changes:
        parts.append(f"the newest recorded change is {_age(changes[0]['age'])}d ago")
    if graph.get("point") is not None:
        parts.append(f"the bug index stays {float(graph['point']):.4f}")
    if pull.get("point") is not None:
        parts.append(f"{pull.get('title_text')} uses that same index")
    if build.get("package"):
        parts.append(f"the build forecast stays on {build['package']}")
    neighbors = graph.get("neighbors") or []
    if neighbors:
        lead = max(neighbors, key=lambda item: (item["bug"], item["module"]))
        parts.append(f"{graph.get('module') or features.get('module')} {lead['direction']} {lead['module']}")
    if survival.get("days") is not None:
        parts.append(f"the latest {survival.get('service')} incident was {_age(survival['days'])}d ago")
    if not parts:
        summary = "No file, commit, or index is available to cite."
    else:
        sentence = ", ".join(parts[:-1]) + (", and " + parts[-1] if len(parts) > 1 else parts[0])
        if len(parts) == 1:
            sentence = parts[0]
        summary = sentence[0].upper() + sentence[1:] + "."
    rows = [{"title": "Cited", "detail": summary}]
    return _step(10, TITLES[9], summary, rows)


def _drift(view: dict, temporal: dict, uncertainty: dict) -> dict:
    changes = [item for item in temporal.get("events") or [] if item["kind"] == "change"]
    recent = sum(1 for item in changes if item["age"] < WINDOW_DAYS)
    older = sum(1 for item in changes if WINDOW_DAYS <= item["age"] < WINDOW_DAYS * 2)
    drifted = recent != older
    outcomes_n = len(view.get("outcomes") or [])
    summary = (
        f"The last {int(WINDOW_DAYS)} days have {recent} changes and the {int(WINDOW_DAYS)} days before that have {older}. "
    )
    if drifted:
        summary += "The recent window differs. "
    else:
        summary += "The two windows match. "
    summary += "Retraining is withheld. These steps do not change the learning rate or the weights."
    if outcomes_n < 3:
        summary += " Fewer than 3 outcomes are recorded."
    return _step(
        11,
        TITLES[10],
        summary,
        [{"title": "windows", "detail": f"recent {recent} · previous {older} · retrained no"}],
        recent=recent,
        older=older,
        drifted=drifted,
        retrained=False,
    )


def _evaluate(view: dict, report: dict, drift: dict) -> dict:
    outcomes = [item for item in view.get("outcomes") or [] if item.get("module")]
    excluded = []
    for fact in _facts(view):
        if fact.get("predicate") != "attributed_to" or fact.get("kind") != "event":
            continue
        name = _pkg_name(str(fact.get("object") or ""))
        if not name or any(item["package"] == name for item in excluded):
            continue
        excluded.append(
            {
                "package": name,
                "reason": f"The build failure attributed to {name} is evidence for the forecast, not a confirmation of it.",
            }
        )
    if not outcomes:
        reading = "unvalidated"
        summary = (
            "No recorded outcome sits on a module, so the predictions stay unvalidated. "
            "A build failure used as evidence is not scored as a hit. The bug index is not adjusted."
        )
    else:
        reading = "compared"
        summary = (
            f"{len(outcomes)} recorded outcomes can be compared later. "
            "They do not adjust the bug index from this page."
        )
    rows = [{"title": reading, "detail": summary}]
    for item in excluded[:2]:
        rows.append({"title": item["package"], "detail": item["reason"]})
    return _step(12, TITLES[11], summary, rows, reading=reading, excluded=excluded, retrained=drift.get("retrained") is False)


def _question(report: dict, key: str) -> list[dict]:
    for item in report.get("questions") or []:
        if item.get("id") == key:
            return list(item.get("rows") or [])
    return []


def _code(view: dict) -> list[dict]:
    return [module for module in view.get("modules") or [] if module.get("role") == "code"]


def _module(view: dict, path: str | None) -> dict | None:
    if not path:
        return None
    return next((item for item in _code(view) if item["path"] == path), None)


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
