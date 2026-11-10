"""Ten counterfactual algorithms, each reading the step before it.

A world first records the variables it would change. An intervention is
do(variable) only where a structural edge says that variable is a cause.
The edge is an assumption until a later outcome supports it. A recorded
failure that the edge was built from does not count as that support.
The draws do not replace the forecast spread, and they do not resimulate
the bug index.
"""

from __future__ import annotations

import random

from app.settings import MONTE_CARLO_SEED
from app.temporal import holds

TRIALS = 200
TITLES = (
    "Counterfactual State Modeling",
    "What-If Scenario Generation",
    "Structural Causal Models (SCMs)",
    "Intervention-Based Causal Inference",
    "Monte Carlo Engineering Simulation",
    "Scenario-Based Risk Estimation",
    "Sensitivity Analysis",
    "Multi-Objective Scenario Optimization",
    "Uncertainty-Aware Outcome Prediction",
    "Counterfactual Outcome Evaluation",
)


def run_algorithms(view: dict, decisions: dict) -> dict:
    state = _state(view, decisions)
    generated = _generate(decisions, state)
    model = _model(view, generated)
    interventions = _intervene(generated, model)
    monte = _monte_carlo(interventions)
    estimated = _estimate(monte)
    sensitivity = _sensitivity(interventions, estimated)
    optimized = _optimize(sensitivity, interventions)
    uncertainty = _uncertainty(view, optimized)
    evaluation = _evaluate(view, model, uncertainty)
    steps = [state, generated, model, interventions, monte, estimated, sensitivity, optimized, uncertainty, evaluation]
    return {
        "steps": steps,
        "note": (
            "The steps run in order. A scenario names the variable it would change. "
            "do(variable) is an intervention only where a structural edge says that variable is a cause. "
            "Changing the number without that edge stays an association. "
            "The edge stays an assumption until a recorded outcome supports it, and the outcome that supplied the edge does not count. "
            "Each world is estimated on its own. The objectives are not added, and the bug index is not resimulated. "
            "The draws do not replace the forecast spread. An index is not a probability."
        ),
    }


def _step(order: int, title: str, summary: str, rows: list[dict], **extra) -> dict:
    return {"order": order, "title": title, "summary": summary, "rows": rows, **extra}


def _state(view: dict, decisions: dict) -> dict:
    scenarios = list(decisions.get("scenarios") or [])
    variables = []
    packages = {item["name"]: item for item in (view.get("dependencies") or {}).get("packages") or []}
    for scenario in scenarios:
        name = scenario.get("package") or ""
        if scenario["index"] == "dependency" and name:
            variables.append(
                {
                    "name": f"{name} upgrade" if scenario["id"] == "withhold_upgrade" else f"{name} build failure",
                    "role": "cause",
                    "factual": "present",
                    "world": "absent",
                    "scenario": scenario["id"],
                }
            )
            package = packages.get(name)
            if package is not None:
                variables.append(
                    {
                        "name": f"{name} dependency index",
                        "role": "effect",
                        "factual": f"{float(package['risk']):.4f}",
                        "world": f"{float(scenario['after']):.4f}",
                        "scenario": scenario["id"],
                    }
                )
        elif scenario["index"] == "likelihood":
            variables.append(
                {
                    "name": f"test gap on {scenario['module']}",
                    "role": "cause",
                    "factual": "untested",
                    "world": "covered",
                    "scenario": scenario["id"],
                }
            )
            variables.append(
                {
                    "name": f"likelihood index on {scenario['module']}",
                    "role": "effect",
                    "factual": f"{float(scenario['before']):.4f}",
                    "world": f"{float(scenario['after']):.4f}",
                    "scenario": scenario["id"],
                }
            )
        elif scenario["index"] == "downstream":
            variables.append(
                {
                    "name": "origin cause",
                    "role": "cause",
                    "factual": "present",
                    "world": "removed",
                    "scenario": scenario["id"],
                }
            )
            variables.append(
                {
                    "name": "downstream index",
                    "role": "effect",
                    "factual": f"{float(scenario['before']):.4f}",
                    "world": f"{float(scenario['after']):.4f}",
                    "scenario": scenario["id"],
                }
            )
    rows = [
        {
            "title": item["name"],
            "detail": f"{item['role']} · factual {item['factual']} · world {item['world']} · {item['scenario']}",
        }
        for item in variables[:8]
    ]
    if variables:
        summary = (
            f"The factual state and {len(scenarios)} worlds record {len(variables)} variables. "
            "A variable is observed here. Setting it is not yet an intervention."
        )
    else:
        summary = "No decision world is available, so there is no factual state to contrast."
    return _step(1, TITLES[0], summary, rows, variables=variables)


def _generate(decisions: dict, state: dict) -> dict:
    scenarios = []
    known = {item["scenario"] for item in state["variables"]}
    for scenario in decisions.get("scenarios") or []:
        if scenario["id"] not in known:
            continue
        cause = next(item["name"] for item in state["variables"] if item["scenario"] == scenario["id"] and item["role"] == "cause")
        scenarios.append({**scenario, "changes": cause, "status": "scenario"})
    rows = [
        {
            "title": item["title"],
            "detail": f"would change {item['changes']} · scenario, not yet an intervention",
        }
        for item in scenarios[:8]
    ]
    if scenarios:
        summary = (
            f"{len(scenarios)} what-if scenarios come from the decision worlds already simulated. "
            "Each one only names the variable it would change."
        )
    else:
        summary = "No what-if scenario can be generated from the current worlds."
    return _step(2, TITLES[1], summary, rows, scenarios=scenarios)


def _model(view: dict, generated: dict) -> dict:
    edges = []
    packages = {scenario.get("package") for scenario in generated["scenarios"] if scenario.get("package")}
    for name in sorted(packages):
        if _failure_follows_upgrade(view, name):
            edges.append(
                {
                    "source": f"{name} upgrade",
                    "target": f"{name} build failure",
                    "status": "assumed",
                    "because": "A recorded build failure is more recent than the upgrade. Withholding the upgrade removes that later failure.",
                }
            )
        edges.append(
            {
                "source": f"{name} upgrade",
                "target": f"{name} dependency index",
                "status": "assumed",
                "because": "The dependency index is propagated from the package facts.",
            }
        )
        edges.append(
            {
                "source": f"{name} build failure",
                "target": f"{name} dependency index",
                "status": "assumed",
                "because": "A recorded build failure is one of the package facts.",
            }
        )
    if any(item["id"] == "add_tests" for item in generated["scenarios"]):
        edges.append(
            {
                "source": "test gap",
                "target": "likelihood index",
                "status": "assumed",
                "because": "The likelihood index uses the test gap of that same file.",
            }
        )
        edges.append(
            {
                "source": "test gap",
                "target": "dependency index",
                "status": "absent",
                "because": "Covering a file with tests does not cause the package index.",
            }
        )
    if any(item["id"] == "contain" for item in generated["scenarios"]):
        edges.append(
            {
                "source": "origin cause",
                "target": "downstream index",
                "status": "assumed",
                "because": "The downstream index is the trace after that cause is removed.",
            }
        )
    rows = [
        {
            "title": f"{edge['source']} → {edge['target']}",
            "detail": f"{edge['status']} · {edge['because']}",
        }
        for edge in edges[:8]
    ]
    assumed = sum(1 for edge in edges if edge["status"] == "assumed")
    if edges:
        summary = (
            f"{assumed} structural edges are assumptions. "
            "An absent edge means changing the source is not a cause of the target."
        )
    else:
        summary = "No structural edge can be stated from the current scenarios."
    return _step(3, TITLES[2], summary, rows, edges=edges)


def _failure_follows_upgrade(view: dict, name: str) -> bool:
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    upgrade_ages = []
    failure_ages = []
    for fact in facts:
        if fact.get("predicate") == "depends_on" and holds(fact, 0):
            if _pkg_name(str(fact.get("object") or "")) != name:
                continue
            opened = float(fact.get("from_age") or 0)
            if 0 < opened <= 90:
                upgrade_ages.append(opened)
        if fact.get("predicate") == "attributed_to" and fact.get("kind") == "event":
            if _pkg_name(str(fact.get("object") or "")) != name:
                continue
            age = fact.get("at_age")
            if age is not None:
                failure_ages.append(float(age))
    if not upgrade_ages or not failure_ages:
        return False
    upgrade_age = min(upgrade_ages)
    return any(age < upgrade_age for age in failure_ages)


def _intervene(generated: dict, model: dict) -> dict:
    assumed = {(edge["source"], edge["target"]) for edge in model["edges"] if edge["status"] == "assumed"}
    interventions = []
    for scenario in generated["scenarios"]:
        kind, cuts, kept = _do_operator(scenario, assumed)
        interventions.append({**scenario, "status": kind, "cuts": cuts, "keeps": kept})
    rows = []
    for item in interventions[:6]:
        cut = ", ".join(item["cuts"]) if item["cuts"] else "no incoming edge"
        rows.append(
            {
                "title": item["title"],
                "detail": f"{item['status']} · do({item['changes']}) · cuts {cut}",
            }
        )
    rows.append(
        {
            "title": "bug index",
            "detail": "association only · these decisions have no edge into the bug index, so it is not resimulated",
        }
    )
    acted = sum(1 for item in interventions if item["status"] == "intervention")
    if interventions:
        summary = (
            f"{acted} scenarios are interventions because a structural edge names the variable as a cause. "
            "A change without that edge would stay an association."
        )
    else:
        summary = "No scenario has a structural edge, so none becomes an intervention."
    return _step(4, TITLES[3], summary, rows, interventions=interventions)


def _do_operator(scenario: dict, assumed: set[tuple[str, str]]) -> tuple[str, list[str], list[str]]:
    if scenario["id"] == "withhold_upgrade":
        name = scenario.get("package") or "package"
        cause = f"{name} upgrade"
        cuts = [f"{source} → {cause}" for source, target in sorted(assumed) if target == cause]
        keeps = [f"{name} build failure → {name} dependency index"]
        if (cause, f"{name} dependency index") in assumed or (cause, f"{name} build failure") in assumed:
            return "intervention", cuts or [f"incoming edges to {cause}"], keeps
        return "association", [], []
    if scenario["id"] == "withhold_failure":
        name = scenario.get("package") or "package"
        cause = f"{name} build failure"
        if (cause, f"{name} dependency index") in assumed:
            return "intervention", [f"{name} upgrade → {cause}"], [f"{name} upgrade → {name} dependency index"]
        return "association", [], []
    if scenario["id"] == "add_tests":
        if ("test gap", "likelihood index") in assumed:
            return "intervention", ["incoming edges to test gap"], ["test gap does not reach the dependency index"]
        return "association", [], []
    if scenario["id"] == "contain":
        if ("origin cause", "downstream index") in assumed:
            return "intervention", ["incoming edges to the origin cause"], ["the package index has no edge from this cut"]
        return "association", [], []
    return "association", [], []


def _monte_carlo(interventions: dict) -> dict:
    draws = {}
    for item in interventions["interventions"]:
        if item["status"] != "intervention":
            continue
        width = 0.08 if item["id"] in {"withhold_upgrade", "withhold_failure"} else 0.14
        point = float(item["after"])
        rng = random.Random(MONTE_CARLO_SEED)
        samples = [
            round(min(1.0, max(0.0, point + rng.uniform(-width, width))), 4)
            for _ in range(TRIALS)
        ]
        draws[item["id"]] = {
            "point": point,
            "width": width,
            "p50": _percentile(samples, 0.50),
            "p90": _percentile(samples, 0.90),
            "trials": TRIALS,
            "seed": MONTE_CARLO_SEED,
        }
    rows = []
    for item in interventions["interventions"]:
        draw = draws.get(item["id"])
        if draw is None:
            continue
        rows.append(
            {
                "title": item["title"],
                "detail": (
                    f"{draw['trials']} draws · seed {draw['seed']} · "
                    f"p50 {draw['p50']:.4f} · p90 {draw['p90']:.4f} · noise ±{draw['width']:.2f}"
                ),
            }
        )
    if draws:
        summary = (
            f"{TRIALS} draws use seed {MONTE_CARLO_SEED} around each intervened index. "
            "They do not replace the forecast spread, and they are not a probability of failure."
        )
    else:
        summary = "No intervention is available to draw. The forecast spread stays as it is."
    return _step(5, TITLES[4], summary, rows[:8], draws=draws, trials=TRIALS, seed=MONTE_CARLO_SEED)


def _estimate(monte: dict) -> dict:
    estimates = []
    for item_id, draw in monte["draws"].items():
        estimates.append({"id": item_id, "estimate": draw["p50"], "point": draw["point"]})
    rows = [
        {
            "title": item["id"],
            "detail": f"risk estimate {item['estimate']:.4f} · this world only · the point index is {item['point']:.4f}",
        }
        for item in estimates[:8]
    ]
    if estimates:
        summary = (
            f"{len(estimates)} intervened worlds each keep their own risk estimate. "
            "The estimates are not added across worlds."
        )
    else:
        summary = "No intervened world has a risk estimate."
    return _step(6, TITLES[5], summary, rows, estimates=estimates)


def _sensitivity(interventions: dict, estimated: dict) -> dict:
    by_id = {item["id"]: item for item in interventions["interventions"] if item["status"] == "intervention"}
    checks = []
    rows = []
    for estimate in estimated["estimates"]:
        scenario = by_id.get(estimate["id"])
        if scenario is None:
            continue
        before = float(scenario["before"])
        after = float(scenario["after"])
        delta = after - before
        half = round(before + 0.5 * delta, 4)
        strong = round(min(1.0, max(0.0, before + 1.5 * delta)), 4)
        stable = _same_side(before, after, half) and _same_side(before, after, strong)
        detail = f"half effect {half:.4f} · recorded {after:.4f} · stronger effect {strong:.4f}"
        if scenario["id"] == "add_tests" and before:
            mild = round(before * 0.55, 4)
            deep = round(before * 0.35, 4)
            stable = stable and mild < before and deep < before
            detail += f" · test factors 0.55 → {mild:.4f} and 0.35 → {deep:.4f}"
        checks.append({"id": scenario["id"], "stable": stable, "title": scenario["title"]})
        label = "stable" if stable else "fragile"
        rows.append({"title": scenario["title"], "detail": f"{label} · {detail}"})
    if checks and all(item["stable"] for item in checks):
        summary = "The direction holds at half effect and at a stronger effect. The sign does not flip."
    elif checks:
        summary = "At least one world changes sign when its effect is halved or strengthened."
    else:
        summary = "No intervention is available for a sensitivity check."
    return _step(7, TITLES[6], summary, rows[:8], checks=checks)


def _same_side(before: float, recorded: float, other: float) -> bool:
    return (recorded - before) * (other - before) > 0


def _optimize(sensitivity: dict, interventions: dict) -> dict:
    by_id = {item["id"]: item for item in interventions["interventions"]}
    groups: dict[str, list[dict]] = {}
    for check in sensitivity["checks"]:
        scenario = by_id.get(check["id"])
        if scenario is None:
            continue
        groups.setdefault(scenario["index"], []).append(scenario)
    chosen = []
    for index, items in sorted(groups.items()):
        pick = min(items, key=lambda item: (float(item["after"]), item["title"]))
        chosen.append(
            {
                "index": index,
                "index_label": pick["index_label"],
                "id": pick["id"],
                "title": pick["title"],
                "after": float(pick["after"]),
            }
        )
    rows = [
        {
            "title": item["index_label"],
            "detail": f"{item['title']} · after {item['after']:.4f} · this objective only",
        }
        for item in chosen
    ]
    if chosen:
        summary = (
            f"{len(chosen)} objectives each keep the world with the lower index. "
            "The objectives are not added, and they are not folded into the regret score."
        )
    else:
        summary = "No objective has an intervened world."
    return _step(8, TITLES[7], summary, rows, chosen=chosen)


def _uncertainty(view: dict, optimized: dict) -> dict:
    chosen = list(optimized.get("chosen") or [])
    if not chosen:
        return _step(
            9,
            TITLES[8],
            "No chosen world has an index to bound. The band would not be a probability.",
            [],
            point=None,
            low=None,
            high=None,
            width=None,
            scenario="",
        )
    dependency = next((item for item in chosen if item["index"] == "dependency"), None)
    focus = dependency or chosen[0]
    point = float(focus["after"])
    observed = focus["id"] in {"withhold_upgrade", "withhold_failure"}
    width = 0.08 if observed else 0.14
    parts = ["observed fact 0.08" if observed else "unobserved action 0.14"]
    if len(view.get("outcomes") or []) < 3:
        width += 0.04
        parts.append("fewer than 3 outcomes +0.04")
    width += 0.04
    parts.append("edge still an assumption +0.04")
    width = round(min(0.24, max(0.06, width)), 4)
    low = round(max(0.0, point - width), 4)
    high = round(min(1.0, point + width), 4)
    summary = (
        f"The band around {focus['title']} is {low:.2f}–{high:.2f} on the index {point:.4f}. "
        "It is not a probability."
    )
    rows = [
        {
            "title": focus["title"],
            "detail": f"index {point:.4f} · band {low:.2f}–{high:.2f} · width {width:.2f} · " + " · ".join(parts),
        }
    ]
    return _step(
        9,
        TITLES[8],
        summary,
        rows,
        point=point,
        low=low,
        high=high,
        width=width,
        scenario=focus["id"],
    )


def _evaluate(view: dict, model: dict, uncertainty: dict) -> dict:
    outcomes = list(view.get("outcomes") or [])
    excluded = _excluded(view, model)
    assumed = [edge for edge in model["edges"] if edge["status"] == "assumed"]
    if not outcomes:
        reading = "unvalidated"
        summary = (
            "The causal edges stay assumptions: no recorded outcome sits on a reached module. "
            "A failure used to draw an edge is evidence, not confirmation. The package index is not adjusted."
        )
    else:
        reading = "compared"
        summary = (
            f"{len(outcomes)} recorded outcomes can be compared with the factual state. "
            "They do not by themselves confirm the alternative world. The package index is not adjusted."
        )
    rows = [{"title": reading, "detail": summary}]
    for item in excluded[:2]:
        rows.append({"title": item["package"], "detail": item["reason"]})
    if assumed:
        rows.append(
            {
                "title": f"{len(assumed)} assumed edges",
                "detail": "Each edge remains an assumption until an outcome that did not create it supports the edge.",
            }
        )
    return _step(
        10,
        TITLES[9],
        summary,
        rows[:4],
        reading=reading,
        excluded=excluded,
        assumed=len(assumed),
        scenario=uncertainty.get("scenario") or "",
    )


def _excluded(view: dict, model: dict) -> list[dict]:
    packages = set()
    for edge in model["edges"]:
        if edge["target"].endswith("build failure") or edge["source"].endswith("build failure"):
            packages.add(edge["source"].replace(" upgrade", "").replace(" build failure", ""))
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    rows = []
    seen = set()
    for fact in facts:
        if fact.get("predicate") != "attributed_to" or fact.get("kind") != "event":
            continue
        name = _pkg_name(str(fact.get("object") or ""))
        if name not in packages or name in seen:
            continue
        seen.add(name)
        rows.append(
            {
                "package": name,
                "reason": f"The build failure attributed to {name} is why the edge exists, so it is not confirmation.",
            }
        )
    return rows


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = int(round(quantile * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


def _pkg_name(label: str) -> str:
    if label.startswith("@") and label.count("@") >= 2:
        return label.rsplit("@", 1)[0]
    if "@" in label and not label.startswith("@"):
        return label.split("@", 1)[0]
    return label

