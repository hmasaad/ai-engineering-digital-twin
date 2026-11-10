"""Calibration and accuracy of the twin.

Recorded outcomes say whether a predicted index matched what happened.
Production checks from history say whether an incident index matched a deploy
or an incident. A forecast built from a failure is not counted as proof it
predicted that failure. The trust value is what the decision engine is
allowed to do.
"""

from __future__ import annotations

from app.fidelity import history_checks
from app.optimize import reliability

JUDGED = {"agreed", "missed", "false_alarm"}


def evaluate(view: dict, raw: dict | None = None) -> dict:
    outcomes = list(view.get("outcomes") or [])
    curve = reliability(outcomes)
    checks = history_checks(view, raw or {})
    counts = {name: sum(1 for check in checks if check.get("verdict") == name) for name in ("agreed", "missed", "false_alarm", "uncertain", "recorded")}
    judged = [check for check in checks if check.get("verdict") in JUDGED]
    hits = counts["agreed"]
    accuracy = None if not judged else round(hits / len(judged), 4)
    brier = None
    if outcomes:
        total = 0.0
        for outcome in outcomes:
            happened = 1.0 if outcome.get("happened") else 0.0
            total += (float(outcome.get("predicted") or 0.0) - happened) ** 2
        brier = round(total / len(outcomes), 4)
    trust, reading = _trust(curve, counts)
    return {
        "trust": trust,
        "reading": reading,
        "summary": _summary(trust, reading, curve, counts),
        "outcomes": {
            "n": len(outcomes),
            "brier": brier,
            "confirmed": sum(1 for outcome in outcomes if outcome.get("happened")),
            "gap": curve.get("gap"),
            "bins": curve.get("bins") or [],
            "note": curve.get("note") or "",
        },
        "checks": {
            "n": len(judged),
            "accuracy": accuracy,
            **counts,
        },
        "note": (
            "Accuracy compares a predicted index with a later outcome or a production check. "
            "A dependency forecast that was built from a recorded build failure is not scored as a hit for that failure. "
            "Hold means the engine must not act. Measure means it may ask for a result. Ready means it may recommend an action."
        ),
    }


def _trust(curve: dict, counts: dict) -> tuple[str, str]:
    if counts["missed"] or counts["false_alarm"]:
        return "hold", "contradicted"
    agreed_production = counts["agreed"] > 0
    if curve.get("n", 0) < 3 and not agreed_production:
        return "hold", "uncalibrated"
    gap = curve.get("gap")
    if curve.get("n", 0) >= 3 and gap is not None and abs(float(gap)) > 0.15:
        return "measure", "biased"
    if curve.get("n", 0) < 3:
        return "measure", "thin"
    return "ready", "aligned"


def _summary(trust: str, reading: str, curve: dict, counts: dict) -> str:
    contradicted = counts["missed"] + counts["false_alarm"]
    if reading == "contradicted":
        noun = "check" if contradicted == 1 else "checks"
        return f"Accuracy is contradicted: {contradicted} production {noun} missed or raised a false alarm."
    if reading == "uncalibrated":
        return "Accuracy is uncalibrated: fewer than 3 recorded outcomes and no agreed production check."
    if reading == "biased":
        return f"The index is biased by {float(curve.get('gap') or 0):.2f} against recorded outcomes. Measure before acting."
    if reading == "thin":
        return "A production check agreed, and recorded outcomes are still thin. Measure before acting."
    return f"Accuracy is aligned across {curve.get('n', 0)} outcomes."
