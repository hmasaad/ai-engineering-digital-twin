"""Prospective engineering decisions, each in its own world.

A withheld upgrade or build failure reuses the contrary world already
propagated. Covering a file with tests changes only that file's likelihood
index. The containment cut is the one already chosen on the trace. Worlds
are not stacked. The bug index is not resimulated, and a world is not
evidence the decision would ship.
"""

from __future__ import annotations


def simulate_decisions(view: dict) -> dict:
    scenarios = []
    scenarios.extend(_withheld(view))
    covered = _cover_tests(view)
    if covered is not None:
        scenarios.append(covered)
    cut = _containment(view)
    if cut is not None:
        scenarios.append(cut)
    report = {
        "scenarios": scenarios,
        "summary": _summary(scenarios),
        "writes_repository": False,
        "note": (
            "Each decision is its own world, simulated before it is taken. "
            "A withheld upgrade or build failure reuses the contrary dependency world. "
            "Tests change only that file's likelihood index. "
            "The containment cut is the one already chosen on the trace. "
            "Worlds are not stacked. The bug index is not resimulated. "
            "A world is not evidence the decision would ship, and a likelihood index is not a probability. "
            "Ten algorithms then separate a scenario from an intervention: a changed variable is a cause only where a structural edge says so, and that edge stays an assumption until a recorded outcome supports it."
        ),
    }
    from app.decision_algorithms import run_algorithms

    report["algorithms"] = run_algorithms(view, report)
    return report


def _withheld(view: dict) -> list[dict]:
    scenarios = []
    for case in (view.get("counterfactual") or {}).get("cases") or []:
        factual = float((case.get("factual") or {}).get("risk") or 0)
        for world in case.get("worlds") or []:
            after = float(world.get("risk") or 0)
            moved = [row for row in world.get("modules") or [] if abs(float(row.get("delta") or 0)) >= 0.0001]
            consequences = [
                {
                    "module": row["module"],
                    "detail": (
                        f"The dependency index moves from {float(row['before']):.4f} to {float(row['after']):.4f}."
                    ),
                }
                for row in moved[:4]
            ]
            if not consequences:
                consequences = [
                    {
                        "module": case.get("package") or "",
                        "detail": f"The package index moves from {factual:.4f} to {after:.4f}.",
                    }
                ]
            kind = world.get("id") or ""
            name = case.get("package") or "the package"
            scenarios.append(
                {
                    "id": kind,
                    "title": world.get("title") or kind,
                    "decision": _decision(kind, name),
                    "contrary_to": world.get("contrary_to") or "",
                    "assumption": _assumption(kind),
                    "index": "dependency",
                    "index_label": "dependency index",
                    "before": factual,
                    "after": after,
                    "delta": round(after - factual, 4),
                    "module": moved[0]["module"] if moved else "",
                    "package": name,
                    "consequences": consequences,
                }
            )
    return scenarios


def _assumption(kind: str) -> str:
    if kind == "withhold_upgrade":
        return "Only that upgrade is removed, and a build failure that came after it leaves with it. The bug index stays."
    if kind == "withhold_failure":
        return "Only the recorded build failure is removed. The upgrade stays. The bug index stays."
    return "Only that recorded fact is removed. The bug index stays."


def _decision(kind: str, name: str) -> str:
    if kind == "withhold_upgrade":
        return f"Do not take the {name} upgrade."
    if kind == "withhold_failure":
        return f"Do not attribute the build failure to {name}."
    return f"Choose a different fact for {name}."


def _cover_tests(view: dict) -> dict | None:
    by_path = {
        module["path"]: module
        for module in view.get("modules") or []
        if module.get("role") == "code"
    }
    target = _untested_target(view, by_path)
    if target is None:
        return None
    path, row, module = target
    before = float(row["likelihood"])
    gap = float(module.get("test_gap") or 0)
    evidence = 0.45 + 0.55 * gap
    after = round(before * 0.45 / evidence, 4) if evidence else before
    if after >= before - 1e-9:
        return None
    trace = _lead_trace(view)
    stayed = ""
    if trace is not None:
        other = next(
            (
                item
                for item in trace.get("affected") or []
                if item["module"] != path and item.get("depth", 0) > 0
            ),
            None,
        )
        if other is not None:
            stayed = f" {other['module']} keeps likelihood {float(other['likelihood']):.4f}."
    return {
        "id": "add_tests",
        "title": f"Cover {path} with a test",
        "decision": f"Add a test for {path} before the weakness spreads further.",
        "contrary_to": "No characterization test is linked to this file.",
        "assumption": "The test changes this file's likelihood index only. Importers keep their own test condition. The package index stays.",
        "index": "likelihood",
        "index_label": "likelihood index",
        "before": before,
        "after": after,
        "delta": round(after - before, 4),
        "module": path,
        "package": "",
        "consequences": [
            {
                "module": path,
                "detail": f"The likelihood index moves from {before:.4f} to {after:.4f}.{stayed}",
            }
        ],
    }


def _untested_target(view: dict, by_path: dict) -> tuple[str, dict, dict] | None:
    failures = (view.get("engineering_failures") or {}).get("failures") or []
    traces = (view.get("dependencies") or {}).get("traces") or []
    candidates = []
    for failure in failures:
        path = failure.get("module") or ""
        module = by_path.get(path)
        if module is None or float(module.get("test_gap") or 0) < 0.5:
            continue
        row = _likelihood_row(traces, path)
        if row is not None:
            candidates.append((path, row, module))
    if candidates:
        return candidates[0]
    trace = _lead_trace(view)
    if trace is None:
        return None
    for row in trace.get("affected") or []:
        module = by_path.get(row["module"])
        if module is None or float(module.get("test_gap") or 0) < 0.5:
            continue
        return row["module"], row, module
    return None


def _likelihood_row(traces: list[dict], path: str) -> dict | None:
    found = None
    for trace in traces:
        for row in trace.get("affected") or []:
            if row.get("module") != path:
                continue
            if found is None or float(row.get("likelihood") or 0) > float(found.get("likelihood") or 0):
                found = row
    return found


def _containment(view: dict) -> dict | None:
    steps = ((view.get("dependencies") or {}).get("algorithms") or {}).get("steps") or []
    cut = next((step for step in steps if step.get("order") == 8), None)
    if cut is None or not cut.get("chosen"):
        return None
    before = float(cut.get("before") or 0)
    after = float(cut.get("after") or 0)
    if before - after < 0.0001:
        return None
    trace = _lead_trace(view)
    consequences = [
        {
            "module": trace["origin"] if trace else "",
            "detail": f"The downstream index moves from {before:.4f} to {after:.4f}.",
        }
    ]
    if trace is not None:
        for row in trace.get("affected") or []:
            if row.get("depth", 0) == 0:
                continue
            consequences.append({"module": row["module"], "detail": row.get("consequence") or ""})
            if len(consequences) >= 4:
                break
    return {
        "id": "contain",
        "title": cut["chosen"],
        "decision": "Take the single containment cut already chosen on the trace.",
        "contrary_to": "The cause stays in place and keeps propagating through importers.",
        "assumption": "This is one cut, not a stack of actions. The repository is not edited. The package index stays.",
        "index": "downstream",
        "index_label": "downstream index",
        "before": before,
        "after": after,
        "delta": round(after - before, 4),
        "module": trace["origin"] if trace else "",
        "package": "",
        "consequences": consequences,
    }


def _lead_trace(view: dict) -> dict | None:
    traces = (view.get("dependencies") or {}).get("traces") or []
    return traces[0] if traces else None


def _summary(scenarios: list[dict]) -> str:
    if not scenarios:
        return "No alternative engineering decision can be simulated from the current traces."
    upgrade = next((item for item in scenarios if item["id"] == "withhold_upgrade"), None)
    if upgrade is not None:
        return (
            f"If the {upgrade['package']} upgrade did not happen, its dependency index would be "
            f"{upgrade['after']:.2f} instead of {upgrade['before']:.2f}."
        )
    cut = next((item for item in scenarios if item["id"] == "contain"), None)
    if cut is not None:
        return (
            f"If the containment cut were taken, the downstream index would move from "
            f"{cut['before']:.2f} to {cut['after']:.2f}."
        )
    tests = scenarios[0]
    return (
        f"If {tests['module']} were covered by a test, its likelihood index would move from "
        f"{tests['before']:.2f} to {tests['after']:.2f}."
    )
