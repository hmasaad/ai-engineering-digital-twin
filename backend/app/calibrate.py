"""Six comparisons between the twin and the recorded engineering system.

Architecture and dependencies are read from the scan the fidelity graph already
counted. Simulations are not treated as outcomes. A failure or incident that
fed an index is not scored as a hit for that index. The accuracy trust and
the learning rate are read and left unchanged.
"""

from __future__ import annotations


def compare(view: dict, raw: dict, graph: dict) -> dict:
    comparisons = [
        _architecture(view, raw, graph),
        _dependencies(graph),
        _simulations(view),
        _failures(view),
        _incidents(view),
        _builds(view),
    ]
    accuracy = view.get("accuracy") or {}
    return {
        "comparisons": comparisons,
        "summary": _summary(comparisons),
        "trust": accuracy.get("trust"),
        "reading": accuracy.get("reading"),
        "note": (
            "These comparisons read the scan, the import graph, recorded history, and the accuracy trust already on the twin. "
            "They do not change that trust, the learning rate, or the bug index. "
            "A simulation is not evidence that a deploy will succeed. "
            "A failure or incident that fed an index is not scored as a hit for that index. "
            "An index is not a calibrated probability."
        ),
    }


def _architecture(view: dict, raw: dict, graph: dict) -> dict:
    scanned = [module for module in raw.get("modules") or [] if module.get("role") == "code"]
    represented = [module for module in view.get("modules") or [] if module.get("role") == "code"]
    if not scanned and not represented:
        return _row(
            "architecture",
            "Architecture",
            "Actual software architecture vs. represented architecture",
            "unmeasured",
            "No scanned module is recorded, so the represented architecture cannot be compared.",
        )
    coverage = graph.get("coverage")
    unresolved = int(graph.get("unresolved_count") or 0)
    dangling = int(graph.get("dangling") or 0)
    if coverage is None:
        reading = "unmeasured"
        detail = "Import coverage is not on this scan yet. Edge count alone cannot show the imports the scanner dropped."
    elif unresolved == 0 and dangling == 0:
        reading = "aligned"
        detail = (
            f"{len(represented)} represented modules match the {len(scanned)} scanned code files. "
            f"Coverage is {float(coverage):.2f}. The bound imports are in the graph."
        )
    else:
        reading = "partial"
        detail = (
            f"Coverage is {float(coverage):.2f}. {unresolved} imports did not bind"
            f" and {dangling} edges leave the scanned files."
        )
    return _row(
        "architecture",
        "Architecture",
        "Actual software architecture vs. represented architecture",
        reading,
        detail,
    )


def _dependencies(graph: dict) -> dict:
    undeclared = list(graph.get("undeclared") or [])
    unused = list(graph.get("unused") or [])
    undeclared_count = int(graph.get("undeclared_count") or 0)
    unused_count = int(graph.get("unused_count") or 0)
    external = int(graph.get("external_count") or 0)
    if external == 0 and undeclared_count == 0 and unused_count == 0:
        return _row(
            "dependencies",
            "Dependencies",
            "Actual dependencies vs. graph dependencies",
            "unmeasured",
            "No package manifest and no external import are recorded.",
        )
    if undeclared_count == 0 and unused_count == 0:
        reading = "aligned"
        detail = "Every external import is declared, and every declared package is imported. The twin does not install them."
    else:
        reading = "partial"
        missing = ", ".join(undeclared[:3]) or "none listed"
        idle = ", ".join(unused[:3]) or "none listed"
        detail = (
            f"{undeclared_count} imported names are missing from the manifest ({missing}). "
            f"{unused_count} manifest entries have no import ({idle}). "
            "The twin does not install them, and this is not the build graph."
        )
    return _row(
        "dependencies",
        "Dependencies",
        "Actual dependencies vs. graph dependencies",
        reading,
        detail,
    )


def _simulations(view: dict) -> dict:
    worlds = 0
    named = set()
    for case in (view.get("counterfactual") or {}).get("cases") or []:
        for world in case.get("worlds") or []:
            worlds += 1
            for row in world.get("modules") or []:
                if row.get("module"):
                    named.add(row["module"])
    for scenario in ((view.get("counterfactual") or {}).get("decisions") or {}).get("scenarios") or []:
        worlds += 1
        if scenario.get("module"):
            named.add(scenario["module"])
    outcomes = [item for item in view.get("outcomes") or [] if item.get("module") in named]
    if worlds == 0:
        detail = "No simulated world is on the twin."
        reading = "unmeasured"
    elif not outcomes:
        detail = (
            f"{worlds} simulated worlds are on the twin and no recorded outcome sits on them. "
            "A simulation is not evidence that a deploy will succeed."
        )
        reading = "unmeasured"
    else:
        gap = sum(float(item.get("predicted") or 0) - (1.0 if item.get("happened") else 0.0) for item in outcomes) / len(outcomes)
        detail = (
            f"{len(outcomes)} recorded outcomes sit on a simulated module. The mean gap is {gap:.2f}. "
            "The simulated index is not itself the outcome."
        )
        reading = "compared"
    return _row(
        "simulations",
        "Simulations",
        "Simulated outcomes vs. real outcomes",
        reading,
        detail,
    )


def _failures(view: dict) -> dict:
    predicted = [
        row.get("package")
        for row in (view.get("engineering_failures") or {}).get("failures") or []
        if row.get("package")
    ]
    evidence = set()
    for fact in _facts(view):
        if fact.get("predicate") == "attributed_to" and fact.get("kind") == "event":
            name = _pkg_name(str(fact.get("object") or ""))
            if name:
                evidence.add(name)
    if not predicted and not evidence:
        return _row(
            "failures",
            "Failures",
            "Predicted failures vs. observed failures",
            "unmeasured",
            "No failure is forecast and no build failure is recorded.",
        )
    forecast_names = sorted(set(predicted))
    if forecast_names and set(forecast_names) <= evidence:
        names = ", ".join(forecast_names)
        return _row(
            "failures",
            "Failures",
            "Predicted failures vs. observed failures",
            "unconfirmed",
            f"The forecast names {names}. The recorded failure attributed to it is the evidence, so it is not scored as a hit.",
        )
    if predicted and not evidence:
        lead = predicted[0]
        return _row(
            "failures",
            "Failures",
            "Predicted failures vs. observed failures",
            "unmeasured",
            f"A failure is forecast through {lead}. No later failure is recorded to compare it with.",
        )
    return _row(
        "failures",
        "Failures",
        "Predicted failures vs. observed failures",
        "unconfirmed",
        "Recorded failures and forecasts do not yet form a later check. A failure used as evidence is not a hit.",
    )


def _incidents(view: dict) -> dict:
    services = []
    for fact in _facts(view):
        if fact.get("predicate") == "incident_on" and fact.get("kind") == "event":
            service = str(fact.get("object") or "")
            if service and service not in services:
                services.append(service)
    if not services:
        return _row(
            "incidents",
            "Incidents",
            "Estimated risks vs. actual engineering incidents",
            "unmeasured",
            "No incident is recorded, so the incident index has no later incident to meet.",
        )
    shown = ", ".join(services[:3])
    return _row(
        "incidents",
        "Incidents",
        "Estimated risks vs. actual engineering incidents",
        "unconfirmed",
        f"Incidents are recorded on {shown}. The incident index uses that history, so the record is not confirmation that the index predicted them.",
    )


def _builds(view: dict) -> dict:
    recorded = []
    for fact in _facts(view):
        if fact.get("predicate") == "failed" and fact.get("kind") == "event":
            recorded.append(str(fact.get("subject") or "build"))
    forecast = [
        row.get("package")
        for row in (view.get("engineering_failures") or {}).get("failures") or []
        if row.get("mode") == "build" and row.get("package")
    ]
    if not recorded:
        return _row(
            "builds",
            "Builds",
            "Expected build performance vs. actual CI/CD performance",
            "unmeasured",
            "No CI result is recorded. The twin has pass and fail results when history stores them, not build durations.",
        )
    names = ", ".join(sorted(set(forecast))[:3]) or "no package"
    listed = ", ".join(recorded[:2])
    return _row(
        "builds",
        "Builds",
        "Expected build performance vs. actual CI/CD performance",
        "unconfirmed",
        (
            f"CI recorded {listed} as a failure. The build forecast names {names}. "
            "That record is the evidence for the forecast, not a measure of duration, and not a confirmation."
        ),
    )


def _row(key: str, title: str, question: str, reading: str, detail: str) -> dict:
    return {"id": key, "title": title, "question": question, "reading": reading, "detail": detail}


def _summary(comparisons: list[dict]) -> str:
    counts = {}
    for item in comparisons:
        counts[item["reading"]] = counts.get(item["reading"], 0) + 1
    aligned = counts.get("aligned", 0)
    partial = counts.get("partial", 0)
    unconfirmed = counts.get("unconfirmed", 0)
    unmeasured = counts.get("unmeasured", 0)
    return (
        f"{aligned} of 6 comparisons match the scan"
        f", {partial} are partial"
        f", {unconfirmed} use a record that also fed the index"
        f", and {unmeasured} have no later outcome."
    )


def _facts(view: dict) -> list[dict]:
    return ((view.get("dynamics") or {}).get("facts")) or []


def _pkg_name(label: str) -> str:
    if label.startswith("@") and label.count("@") >= 2:
        return label.rsplit("@", 1)[0]
    if "@" in label and not label.startswith("@"):
        return label.split("@", 1)[0]
    return label
