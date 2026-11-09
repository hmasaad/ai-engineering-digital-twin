"""Decision intelligence for one module.

Objectives stay separate until a recommendation has to be chosen. A measurement
is worth taking when it would change that choice, or when the effect of acting
sits inside the forecast band. The plan's second step depends on the outcome.
"""

from __future__ import annotations

OBJECTIVES = ("bug", "incident", "delay", "cycles")
WEIGHTS = {
    "balanced": {"bug": 1.0, "incident": 0.8, "delay": 0.5, "cycles": 0.4},
    "defect": {"bug": 2.0, "incident": 0.3, "delay": 0.15, "cycles": 0.2},
    "incident": {"bug": 0.3, "incident": 2.0, "delay": 0.15, "cycles": 0.2},
    "delay": {"bug": 0.3, "incident": 0.3, "delay": 2.0, "cycles": 0.2},
}
PROBES = {
    "defect": "Learn whether a defect is the failure to avoid.",
    "incident": "Learn whether an incident is the failure to avoid.",
    "delay": "Learn whether a schedule slip is acceptable.",
}
MECHANISM = {
    "add_tests": "The intervention closes the test gap. Defect pressure falls where tests meet active code.",
    "freeze": "The intervention suppresses churn. Defect pressure falls, and the freeze itself raises delay.",
    "add_circuit_breaker": "The intervention lowers incident pressure on the whole service. The file is not rewritten.",
    "add_owner": "The intervention adds an owner. Narrow-ownership pressure falls. Onboarding time is not modeled.",
    "extract_module": "The intervention removes cross-service edges touching the file and lowers complexity.",
    "none": "No intervention. The file stays on the path that already happened.",
}


def analyze(report: dict, width: float) -> dict:
    """Build the decision from a counterfactual report and a forecast band width."""
    options = _options(report)
    baseline = next(item for item in options if item["action"] == "none")
    front = _pareto(options, baseline)
    balanced = _winner(options, WEIGHTS["balanced"], baseline)
    probes = _probes(options, balanced, baseline)
    lead = probes[0]
    mode = _mode(report["factual"])
    gain = _gain(baseline, balanced, mode)
    band = max(0.0, float(width))
    distinguished = gain > band + 1e-9
    if distinguished:
        status = "act"
        recommendation = balanced
        because = (
            f"{balanced['title']} is the balanced choice and its {mode} effect of {gain:.2f} "
            f"sits outside the {band:.2f} uncertainty band."
        )
    else:
        status = "measure"
        recommendation = balanced
        because = (
            f"{balanced['title']} is the balanced choice, but its {mode} effect of {gain:.2f} "
            f"sits inside the {band:.2f} uncertainty band. Measure before acting."
        )
    return {
        "module": report["module"],
        "status": status,
        "mode": mode,
        "width": round(band, 4),
        "gain": round(gain, 4),
        "recommendation": _public(recommendation, baseline),
        "because": because,
        "pareto": [_public(item, baseline) for item in front],
        "dominated": [_public(item, baseline) for item in options if item["action"] not in {row["action"] for row in front}],
        "probes": probes,
        "information": {
            "question": lead["question"],
            "value": lead["loss"],
            "changes_decision": lead["changes_decision"],
            "action_if_resolved": lead["title"],
            "note": _information_note(lead, balanced),
        },
        "causal": [_causal(report["module"], item) for item in front],
        "plan": _plan(status, balanced, options, mode, lead, baseline),
    }


def _options(report: dict) -> list[dict]:
    factual = report["factual"]
    cycles = int(factual["metrics"]["cycles"])
    options = [
        {
            "action": "none",
            "title": "Leave it as it is",
            "local": {"bug": factual["bug"], "incident": factual["incident"], "delay": factual["delay"]},
            "cycles": cycles,
            "deltas": {"expected_bug_modules": 0.0, "expected_incident_services": 0.0, "delay": 0.0, "cycles": 0},
            "note": MECHANISM["none"],
        }
    ]
    for item in report["alternatives"]:
        options.append(
            {
                "action": item["action"],
                "title": item["title"],
                "local": dict(item["local"]),
                "cycles": int(item["cycles"]),
                "deltas": {
                    "expected_bug_modules": float(item["deltas"]["expected_bug_modules"]),
                    "expected_incident_services": float(item["deltas"]["expected_incident_services"]),
                    "delay": float(item["deltas"]["delay"]),
                    "cycles": int(item["deltas"]["cycles"]),
                },
                "note": item["note"],
            }
        )
    return options


def _pareto(options: list[dict], baseline: dict) -> list[dict]:
    front = []
    for candidate in options:
        if any(_dominates(other, candidate) for other in options if other["action"] != candidate["action"]):
            continue
        front.append(candidate)
    front.sort(key=lambda item: (_vector_value(item, WEIGHTS["balanced"], baseline), item["title"]), reverse=True)
    return front


def _dominates(left: dict, right: dict) -> bool:
    better = False
    for key in OBJECTIVES:
        gap = _objective(left, key) - _objective(right, key)
        tolerance = 0 if key == "cycles" else 0.005
        if gap > tolerance:
            return False
        if gap < -tolerance:
            better = True
    return better


def _objective(option: dict, key: str) -> float:
    if key == "cycles":
        return float(option["cycles"])
    return float(option["local"][key])


def _winner(options: list[dict], weights: dict[str, float], baseline: dict) -> dict:
    return max(options, key=lambda item: (_vector_value(item, weights, baseline), item["title"]))


def _vector_value(option: dict, weights: dict[str, float], baseline: dict) -> float:
    """Higher is better. Movement is on this module, relative to leaving it alone."""
    score = 0.0
    for key in ("bug", "incident", "delay"):
        score -= weights[key] * (float(option["local"][key]) - float(baseline["local"][key]))
    score -= weights["cycles"] * (float(option["cycles"]) - float(baseline["cycles"]))
    return round(score, 4)


def _probes(options: list[dict], balanced: dict, baseline: dict) -> list[dict]:
    rows = []
    for name, weights in WEIGHTS.items():
        if name == "balanced":
            continue
        later = _winner(options, weights, baseline)
        loss = round(_vector_value(later, weights, baseline) - _vector_value(balanced, weights, baseline), 4)
        rows.append(
            {
                "objective": name,
                "question": PROBES[name],
                "title": later["title"],
                "action": later["action"],
                "loss": max(0.0, loss),
                "changes_decision": later["action"] != balanced["action"],
            }
        )
    rows.sort(key=lambda item: (item["loss"], item["objective"]), reverse=True)
    return rows


def _mode(factual: dict) -> str:
    return max(("bug", "incident", "delay"), key=lambda key: float(factual[key]))


def _gain(baseline: dict, chosen: dict, mode: str) -> float:
    if chosen["action"] == "none":
        return 0.0
    return max(0.0, float(baseline["local"][mode]) - float(chosen["local"][mode]))


def _plan(status: str, balanced: dict, options: list[dict], mode: str, lead: dict, baseline: dict) -> dict:
    weight_name = {"bug": "defect", "incident": "incident", "delay": "delay"}[mode]
    failure = _winner(options, WEIGHTS[weight_name], baseline)
    if status == "act" and failure["action"] in {balanced["action"], "none"}:
        others = [item for item in options if item["action"] not in {balanced["action"], "none"}]
        failure = _winner(others, WEIGHTS[weight_name], baseline) if others else failure
    held = next(item for item in options if item["action"] == "none")
    if status == "measure":
        if lead["changes_decision"] and lead["loss"] > 0:
            now = {
                "action": "measure",
                "title": lead["question"],
                "because": "That answer changes the balanced choice, and the effect of acting now is still inside the uncertainty band.",
            }
        else:
            now = {
                "action": "measure",
                "title": f"Record whether the {mode} happens.",
                "because": "The effect of acting now sits inside the uncertainty band. One recorded outcome narrows the band before a commitment.",
            }
    else:
        now = {
            "action": balanced["action"],
            "title": balanced["title"],
            "because": "The effect clears the uncertainty band under the balanced objectives.",
        }
    return {
        "now": now,
        "if_failure": {
            "action": failure["action"],
            "title": failure["title"],
            "because": f"If the {mode} lands, take this branch. It is scored for that failure, not for the balanced mix.",
        },
        "if_holds": {
            "action": held["action"],
            "title": held["title"],
            "because": "If the failure does not land, leave the file on its current path.",
        },
    }


def _causal(module: str, option: dict) -> dict:
    return {
        "action": option["action"],
        "title": option["title"],
        "intervention": f"do({option['action']}) on {module}",
        "mechanism": MECHANISM.get(option["action"], option["note"]),
        "effects": {
            "bug": round(float(option["deltas"]["expected_bug_modules"]), 4),
            "incident": round(float(option["deltas"]["expected_incident_services"]), 4),
            "delay": round(float(option["deltas"]["delay"]), 4),
            "cycles": int(option["deltas"]["cycles"]),
        },
        "holds": "The action is applied alone. The rest of the repository stays as it is.",
    }


def _public(option: dict, baseline: dict) -> dict:
    return {
        "action": option["action"],
        "title": option["title"],
        "value": _vector_value(option, WEIGHTS["balanced"], baseline),
        "bug": round(float(option["local"]["bug"]), 4),
        "incident": round(float(option["local"]["incident"]), 4),
        "delay": round(float(option["local"]["delay"]), 4),
        "cycles": int(option["cycles"]),
    }


def _information_note(lead: dict, balanced: dict) -> str:
    if lead["changes_decision"] and lead["loss"] > 0:
        return (
            f"Committing to {balanced['title']} costs {lead['loss']:.2f} if {lead['objective']} is the true objective. "
            f"The choice would then be {lead['title']}."
        )
    return "Knowing which objective matters does not change the balanced choice. The value of that question is about zero."
