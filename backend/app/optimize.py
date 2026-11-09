"""Closed loop: a small policy, a calibration against real outcomes, and the next action.

The simulation proposes a value for each engineering action. Recorded outcomes
then move a tabular action value, which is the reinforcement step. The same
outcomes are binned against the index they were predicted from, and that gap
corrects the next reading.
"""

from __future__ import annotations

from app.simulate import DECISIONS, run_counterfactual

ALPHA = 0.45
BINS = ((0.0, 0.35, "low"), (0.35, 0.65, "mid"), (0.65, 1.01, "high"))


def empty_policy() -> dict:
    return {"q": {}, "visits": {}, "steps": []}


def situation(module: dict) -> str:
    """Coarse state the policy conditions on. Named so a recommendation can be explained."""
    features = module.get("features") or {}
    tags = []
    if float(module.get("test_gap") or 0) >= 0.5:
        tags.append("untested")
    if float(module.get("churn") or 0) >= 80 or float(features.get("churn") or 0) >= 0.5:
        tags.append("hot")
    if module.get("incident_level") or float(features.get("incident_pressure") or 0) >= 0.35:
        tags.append("incident")
    if float(features.get("owner_risk") or 0) >= 0.7:
        tags.append("solo")
    return "+".join(tags) or "quiet"


def reliability(outcomes: list[dict]) -> dict:
    """Predicted index against the fraction of outcomes that actually happened."""
    rows = []
    for low, high, name in BINS:
        group = [item for item in outcomes if low <= float(item.get("predicted") or 0) < high]
        if not group:
            rows.append({"name": name, "low": low, "high": high, "n": 0, "predicted": None, "observed": None, "gap": None})
            continue
        mean_predicted = sum(float(item.get("predicted") or 0) for item in group) / len(group)
        observed = sum(1.0 if item.get("happened") else 0.0 for item in group) / len(group)
        rows.append(
            {
                "name": name,
                "low": low,
                "high": high,
                "n": len(group),
                "predicted": round(mean_predicted, 4),
                "observed": round(observed, 4),
                "gap": round(mean_predicted - observed, 4),
            }
        )
    if outcomes:
        gap = sum(float(item.get("predicted") or 0) - (1.0 if item.get("happened") else 0.0) for item in outcomes) / len(outcomes)
    else:
        gap = None
    return {
        "bins": rows,
        "n": len(outcomes),
        "gap": None if gap is None else round(gap, 4),
        "note": _calibration_note(rows, gap),
    }


def apply_calibration(score: float, report: dict) -> float:
    """Pull a raw index toward the observed rate in its bin. Unseen bins stay put."""
    point = float(score)
    for band in report.get("bins") or []:
        if band["n"] <= 0:
            continue
        if band["low"] <= point < band["high"]:
            weight = band["n"] / (band["n"] + 3)
            adjusted = (1.0 - weight) * point + weight * float(band["observed"])
            return round(max(0.0, min(1.0, adjusted)), 4)
    return round(max(0.0, min(1.0, point)), 4)


def reward(took: bool, happened: bool) -> float:
    """What the outcome was worth to the action that was actually taken."""
    if took and not happened:
        return 1.0
    if took and happened:
        return -1.0
    if happened:
        return -0.6
    return 0.2


def apply_feedback(policy: dict, state: str, action: str, happened: bool, took: bool) -> dict:
    """One tabular update: Q ← Q + α (reward − Q)."""
    current = empty_policy()
    current["q"] = {key: dict(values) for key, values in (policy.get("q") or {}).items()}
    current["visits"] = dict(policy.get("visits") or {})
    current["steps"] = list(policy.get("steps") or [])
    table = current["q"].setdefault(state, {})
    before = float(table.get(action, 0.0))
    payoff = reward(took, happened)
    after = before + ALPHA * (payoff - before)
    table[action] = round(after, 4)
    visit_key = f"{state}|{action}"
    current["visits"][visit_key] = int(current["visits"].get(visit_key, 0)) + 1
    current["steps"].append(
        {
            "situation": state,
            "action": action,
            "took": took,
            "happened": happened,
            "reward": payoff,
            "before": round(before, 4),
            "after": round(after, 4),
        }
    )
    current["steps"] = current["steps"][-12:]
    return current


def build_loop(raw: dict, models: dict | None, module: str, outcomes: list[dict], policy: dict | None) -> dict:
    stored = policy or empty_policy()
    report = run_counterfactual(raw, models, module)
    state = situation(_target_view(raw, models, module, report))
    learned = (stored.get("q") or {}).get(state) or {}
    visits = stored.get("visits") or {}
    actions = []
    for action, title in list(DECISIONS) + [("none", "Leave it as it is")]:
        alternative = next((item for item in report["alternatives"] if item["action"] == action), None)
        prior = 0.0 if alternative is None else round(-float(alternative["regret"]), 4)
        value_learned = round(float(learned.get(action, 0.0)), 4)
        actions.append(
            {
                "action": action,
                "title": title,
                "prior": prior,
                "learned": value_learned,
                "value": round(prior + value_learned, 4),
                "visits": int(visits.get(f"{state}|{action}", 0)),
            }
        )
    actions.sort(key=lambda item: (-item["value"], item["title"]))
    chosen = actions[0]
    mode = "bug"
    raw_score = float(report["factual"]["bug"])
    curve = reliability([item for item in outcomes if item.get("kind", "bug") == "bug"] or outcomes)
    return {
        "module": module,
        "situation": state,
        "mode": mode,
        "raw_score": round(raw_score, 4),
        "calibrated_score": apply_calibration(raw_score, curve),
        "recommendation": chosen,
        "actions": actions,
        "why": (
            f"{chosen['title']} leads for {state or 'this situation'}. "
            f"The simulation values it at {chosen['prior']:.2f}. "
            f"Recorded outcomes have moved it by {chosen['learned']:.2f} over {chosen['visits']} takes."
        ),
        "calibration": curve,
        "steps": list(reversed(stored.get("steps") or []))[:6],
        "alpha": ALPHA,
    }


def _target_view(raw: dict, models: dict | None, module: str, report: dict) -> dict:
    """Situation uses the scored module. Fall back to the counterfactual facts if needed."""
    from app.score import merge_models, observables_from_raw, score_observables

    scored = score_observables(observables_from_raw(raw), raw["edges"], merge_models(models))
    target = next((item for item in scored if item["path"] == module), None)
    if target is not None:
        return target
    return {"path": module, "test_gap": 1, "churn": 0, "features": {}, "risks": {"bug": report["factual"]["bug"]}}


def _calibration_note(rows: list[dict], gap: float | None) -> str:
    if gap is None:
        return "No outcomes yet. The index is uncalibrated, so the reading matches the raw score."
    used = [row for row in rows if row["n"]]
    if not used:
        return "No outcomes yet. The index is uncalibrated, so the reading matches the raw score."
    direction = "high" if gap > 0.05 else "low" if gap < -0.05 else "close"
    if direction == "high":
        return "Recorded outcomes failed less often than the index suggested, so the calibrated reading is pulled down."
    if direction == "low":
        return "Recorded outcomes failed more often than the index suggested, so the calibrated reading is pulled up."
    return "Recorded outcomes sit close to the index, so the calibration barely moves the reading."
