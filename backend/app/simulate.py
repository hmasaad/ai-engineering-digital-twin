"""Hypothetical edits to the twin, rescored with the same model as the live view."""

from __future__ import annotations

import copy
import random
from collections import defaultdict

from app.graph import find_cycles
from app.score import code_edges, merge_models, metrics, observables_from_raw, score_observables
from app.settings import MONTE_CARLO_SEED, MONTE_CARLO_TRIALS

ACTIONS = {"add_tests", "extract_module", "add_owner", "add_circuit_breaker", "freeze"}


def run_scenario(raw: dict, models: dict | None, name: str, interventions: list[dict]) -> dict:
    if not interventions:
        raise KeyError("A scenario needs at least one intervention")
    active = merge_models(models)
    baseline_obs = observables_from_raw(raw)
    edges = [dict(edge) for edge in raw["edges"]]
    baseline = score_observables(baseline_obs, edges, active)
    edited = copy.deepcopy(baseline_obs)
    edited_edges = [dict(edge) for edge in edges]
    notes = []
    targets = []
    for step in interventions:
        action = step.get("action")
        module = step.get("module")
        if action not in ACTIONS:
            raise KeyError(f"Unknown action: {action}")
        edited, edited_edges, note = _apply(edited, edited_edges, action, module)
        notes.append(note)
        targets.append(module)
    simulated = score_observables(edited, edited_edges, active)
    before = metrics(baseline, edges)
    after = metrics(simulated, edited_edges)
    keys = [key for key in before if key != "spread"]
    return {
        "name": name,
        "notes": notes,
        "baseline": {**before, "spread": spread(baseline)},
        "simulated": {**after, "spread": spread(simulated)},
        "deltas": {key: _delta(before[key], after[key]) for key in keys},
        "movers": _movers(baseline, simulated, targets),
        "cycles_before": [cycle for cycle in find_cycles(code_edges(baseline, edges))],
        "cycles_after": [cycle for cycle in find_cycles(code_edges(simulated, edited_edges))],
    }


DECISIONS = (
    ("add_tests", "Add characterization tests"),
    ("freeze", "Freeze further changes"),
    ("add_circuit_breaker", "Add a circuit breaker"),
    ("add_owner", "Add a second owner"),
    ("extract_module", "Extract the shared dependency"),
)


def run_counterfactual(raw: dict, models: dict | None, module: str) -> dict:
    """Score the world that happened against each alternative decision, applied alone."""
    active = merge_models(models)
    edges = [dict(edge) for edge in raw["edges"]]
    baseline = score_observables(observables_from_raw(raw), edges, active)
    target = next((item for item in baseline if item["path"] == module and item["role"] == "code"), None)
    if target is None:
        raise KeyError(f"Unknown module: {module}")
    cycles = find_cycles(code_edges(baseline, edges))
    on_cycle = any(module in cycle for cycle in cycles)
    factual_metrics = metrics(baseline, edges)
    alternatives = []
    for action, title in DECISIONS:
        result = run_scenario(raw, active, title, [{"action": action, "module": module}])
        local = {
            "bug": _local(result, module, "bug", target["risks"]["bug"]),
            "incident": _local(result, module, "incident", target["risks"]["incident"]),
            "delay": _local(result, module, "delay", target["risks"]["delay"]),
        }
        alternatives.append(
            {
                "action": action,
                "title": title,
                "contrary_to": _contrary(action, target, on_cycle),
                "note": result["notes"][0],
                "deltas": result["deltas"],
                "local": local,
                "cycles": result["simulated"]["cycles"],
                "tradeoff": _tradeoff(result["deltas"]),
                "regret": _regret(result["deltas"]),
                "preferred": False,
            }
        )
    alternatives.sort(key=lambda item: (item["regret"], item["title"]))
    alternatives[0]["preferred"] = True
    return {
        "module": module,
        "question": f"What if a different decision had been taken on {module}?",
        "factual": {
            "summary": _factual_summary(target, on_cycle),
            "bug": target["risks"]["bug"],
            "incident": target["risks"]["incident"],
            "delay": target["risks"]["delay"],
            "metrics": factual_metrics,
            "on_cycle": on_cycle,
        },
        "alternatives": alternatives,
        "preferred": alternatives[0]["action"],
        "why": _why(alternatives),
    }


def _contrary(action: str, target: dict, on_cycle: bool) -> str:
    authors = target["authors"]
    if action == "add_tests":
        if target["test_gap"] >= 0.5:
            return "No characterization tests are linked to this file."
        return "Tests are already linked, so this mostly repeats what happened."
    if action == "freeze":
        if target["churn"] >= 40:
            return f"The file kept moving: about {int(round(target['churn']))} decayed lines of churn, and it was not frozen."
        return "The file was already quiet, so a freeze changes little."
    if action == "add_owner":
        if len(authors) < 2:
            who = ", ".join(authors) or "nobody"
            return f"Only {who} is recorded on this file."
        return f"{len(authors)} authors already share this file."
    if action == "add_circuit_breaker":
        summary = target.get("incident_summary")
        if summary:
            return f"Incident pressure stayed on {target['service']}: {summary}"
        return f"No breaker was assumed on {target['service']}."
    if on_cycle:
        return "This file sits on an import cycle that was left in place."
    return "This file is not on a cross-service cycle. The alternative still assumes a split."


def _factual_summary(target: dict, on_cycle: bool) -> str:
    parts = []
    parts.append("no linked tests" if target["test_gap"] >= 0.5 else "tests already linked")
    if target["churn"] >= 40:
        parts.append(f"about {int(round(target['churn']))} lines of churn")
    else:
        parts.append("little recent churn")
    if len(target["authors"]) <= 1:
        parts.append("a single recorded owner" if target["authors"] else "no recorded owner")
    else:
        parts.append(f"{len(target['authors'])} owners")
    if target.get("incident_summary"):
        parts.append("incident pressure still on the service")
    if on_cycle:
        parts.append("a place on an import cycle")
    if len(parts) == 1:
        body = parts[0]
    else:
        body = ", ".join(parts[:-1]) + ", and " + parts[-1]
    return "What happened: " + body + "."


def _tradeoff(deltas: dict) -> str:
    labels = (
        ("expected_bug_modules", "bug-bearing modules"),
        ("expected_incident_services", "incident-bearing services"),
        ("delay", "mean delay"),
        ("cycles", "import cycles"),
    )
    parts = []
    for key, label in labels:
        change = deltas[key]
        if isinstance(change, int):
            if change == 0:
                continue
            parts.append(f"{label} {change:+d}")
        elif abs(float(change)) >= 0.005:
            parts.append(f"{label} {float(change):+.2f}")
    if not parts:
        return "The indexes stay essentially where they are."
    return "Compared with what happened: " + ", ".join(parts) + "."


def _regret(deltas: dict) -> float:
    return round(
        float(deltas["expected_bug_modules"])
        + 0.8 * float(deltas["expected_incident_services"])
        + 0.5 * float(deltas["delay"])
        + 0.4 * float(deltas["cycles"]),
        4,
    )


def _why(alternatives: list[dict]) -> str:
    winner = alternatives[0]
    sentence = f"{winner['title']} has the lowest combined movement across bug, incident, delay, and cycles."
    if float(winner["deltas"]["delay"]) > 0.01:
        sentence += f" Mean delay rises by {float(winner['deltas']['delay']):.2f} under that decision."
    if len(alternatives) > 1:
        sentence += f" Next is {alternatives[1]['title']}."
    return sentence


def _local(result: dict, module: str, metric: str, fallback: float) -> float:
    row = next((item for item in result["movers"] if item["module"] == module and item["metric"] == metric), None)
    if row is None:
        return fallback
    return row["after"]


def spread(modules: list[dict], trials: int = MONTE_CARLO_TRIALS, seed: int = MONTE_CARLO_SEED) -> dict:
    """Draw module and service hits from the current indexes. Fixed seed, so the band is reproducible."""
    code = [module for module in modules if module["role"] == "code"]
    by_service: dict[str, list[dict]] = defaultdict(list)
    for module in code:
        by_service[module["service"]].append(module)
    rng = random.Random(seed)
    bug_counts = []
    incident_counts = []
    for _ in range(trials):
        bug_counts.append(sum(1 for module in code if rng.random() < module["risks"]["bug"]))
        incidents = 0
        for group in by_service.values():
            probability = max(module["risks"]["incident"] for module in group)
            incidents += 1 if rng.random() < probability else 0
        incident_counts.append(incidents)
    return {
        "trials": trials,
        "bug_p50": _percentile(bug_counts, 0.50),
        "bug_p90": _percentile(bug_counts, 0.90),
        "incident_p50": _percentile(incident_counts, 0.50),
        "incident_p90": _percentile(incident_counts, 0.90),
    }


def _apply(observables: list[dict], edges: list[dict], action: str, module: str) -> tuple[list[dict], list[dict], str]:
    target = next((item for item in observables if item["path"] == module and item["role"] == "code"), None)
    if target is None:
        raise KeyError(f"Unknown module: {module}")
    if action == "add_tests":
        target["test_gap"] = min(float(target["test_gap"]), 0.15)
        note = (
            f"Characterization tests cover {module}. Risk falls where tests meet active code. "
            "The model does not charge the time required to write them."
        )
    elif action == "extract_module":
        target["complexity"] = max(1, int(round(target["complexity"] * 0.6)))
        target["intervention_drag"] = max(float(target["intervention_drag"]), 0.45)
        edges = _drop_cross_edges(observables, edges, module)
        note = (
            f"Cross-service imports touching {module} are removed in the hypothetical graph and complexity is reduced. "
            "Delay drag rises while the split is in progress."
        )
    elif action == "add_owner":
        if "second-owner" not in target["authors"]:
            target["authors"] = sorted([*target["authors"], "second-owner"])
        note = f"A second owner is assumed on {module}. The calendar cost of onboarding is not modeled."
    elif action == "add_circuit_breaker":
        service = target["service"]
        for item in observables:
            if item["service"] == service and item["role"] == "code":
                item["incident_raw"] = float(item["incident_raw"]) * 0.55
        target["perf_smells"] = float(target["perf_smells"]) * 0.5
        note = (
            f"A breaker and a retry budget are assumed across {service}. "
            "Incident pressure drops. The repository is not edited."
        )
    elif action == "freeze":
        target["churn"] = float(target["churn"]) * 0.35
        target["intervention_drag"] = max(float(target["intervention_drag"]), 1.0)
        note = f"Churn on {module} is suppressed, so defect pressure falls, and scenario drag raises delay risk."
    else:
        raise KeyError(f"Unknown action: {action}")
    return observables, edges, note


def _drop_cross_edges(observables: list[dict], edges: list[dict], module: str) -> list[dict]:
    services = {item["path"]: item["service"] for item in observables}
    kept = []
    for edge in edges:
        touches = edge["source"] == module or edge["target"] == module
        cross = services.get(edge["source"]) != services.get(edge["target"])
        if touches and cross:
            continue
        kept.append(edge)
    return kept


def _movers(before: list[dict], after: list[dict], targets: list[str]) -> list[dict]:
    later = {module["path"]: module for module in after}
    rows = []
    for module in before:
        if module["role"] != "code":
            continue
        other = later.get(module["path"])
        if other is None:
            continue
        for metric in ("bug", "incident", "delay", "perf", "debt"):
            old = module["risks"][metric] if metric != "debt" else module["debt"]
            new = other["risks"][metric] if metric != "debt" else other["debt"]
            if abs(new - old) < 0.01 and module["path"] not in targets:
                continue
            if abs(new - old) < 0.001:
                continue
            rows.append(
                {
                    "module": module["path"],
                    "metric": metric,
                    "before": round(old, 4),
                    "after": round(new, 4),
                    "delta": round(new - old, 4),
                }
            )
    rows.sort(key=lambda row: abs(row["delta"]), reverse=True)
    return rows[:8]


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = int(round(quantile * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


def _delta(before: float, after: float) -> float | int:
    change = after - before
    if isinstance(before, int) and isinstance(after, int):
        return int(change)
    return round(float(change), 4)
