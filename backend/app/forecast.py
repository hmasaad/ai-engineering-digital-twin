"""Predictive failure forecast and an explainable uncertainty band.

The point score is the existing logistic index. The band widens when the
history behind that index is thin, when the index sits near one half, and when
few outcomes have been recorded. It is not a calibrated probability interval.
"""

from __future__ import annotations

MODES = ("bug", "incident", "delay", "perf")
MODE_LABEL = {
    "bug": "defect",
    "incident": "incident",
    "delay": "delivery slip",
    "perf": "performance degradation",
}


def build_forecast(modules: list[dict], band: dict, outcomes_n: int) -> dict:
    code = [module for module in modules if module.get("role") == "code"]
    rows = [_row(module, outcomes_n) for module in code]
    rows.sort(key=lambda row: (row["score"], row["module"]), reverse=True)
    shown = [row for row in rows if row["score"] >= 0.22][:8] or rows[:5]
    expected_bugs = round(sum(module["risks"]["bug"] for module in code), 4)
    expected_incidents = _expected_incidents(code)
    lead = shown[0] if shown else None
    return {
        "headline": _headline(expected_bugs, expected_incidents, band, lead),
        "expected_bug_modules": expected_bugs,
        "expected_incident_services": expected_incidents,
        "band": {
            "bug_p50": band.get("bug_p50", 0),
            "bug_p90": band.get("bug_p90", 0),
            "incident_p50": band.get("incident_p50", 0),
            "incident_p90": band.get("incident_p90", 0),
            "trials": band.get("trials", 0),
        },
        "failures": shown,
        "by_module": {row["module"]: row for row in rows},
        "confidence": {
            "outcomes": int(outcomes_n),
            "mean_width": round(sum(row["width"] for row in rows) / len(rows), 4) if rows else 0.0,
            "note": _confidence_note(outcomes_n),
        },
    }


def _row(module: dict, outcomes_n: int) -> dict:
    mode = max(MODES, key=lambda kind: float(module["risks"][kind]))
    score = float(module["risks"][mode])
    evidence = _evidence(module)
    width = _width(score, evidence, outcomes_n)
    low = round(max(0.0, score - width), 4)
    high = round(min(1.0, score + width), 4)
    drivers = [
        item["label"]
        for item in (module.get("contributions") or {}).get(mode, [])
        if float(item.get("contribution", 0)) > 0.05
    ][:3]
    horizon = _horizon(module)
    confidence = _confidence(width)
    return {
        "module": module["path"],
        "service": module["service"],
        "mode": mode,
        "mode_label": MODE_LABEL[mode],
        "score": round(score, 4),
        "low": low,
        "high": high,
        "width": round(width, 4),
        "base_width": round(width, 4),
        "agreed_checks": 0,
        "evidence": round(evidence, 4),
        "confidence": confidence,
        "horizon": horizon,
        "drivers": drivers,
        "statement": (
            f"{module['path']} is forecast as a {MODE_LABEL[mode]} at {score:.2f} "
            f"(band {low:.2f}–{high:.2f}, {confidence} confidence), {horizon}."
        ),
    }


def _evidence(module: dict) -> float:
    """Share of the signals that were actually observed, from 0 to 1."""
    observed = [
        1.0 if module.get("authors") or float(module.get("churn") or 0) > 0 else 0.0,
        1.0 if module.get("incident_level") or float(module.get("incident_raw") or 0) > 0 else 0.25,
        1.0 if module.get("authors") else 0.0,
        1.0 if float(module.get("review_hours") or 0) > 0 or float(module.get("rollbacks") or 0) > 0 else 0.2,
        1.0,
    ]
    return sum(observed) / len(observed)


def _width(score: float, evidence: float, outcomes_n: int) -> float:
    decisiveness = abs(2.0 * score - 1.0)
    width = 0.05 + 0.22 * (1.0 - evidence) + 0.10 * (1.0 - decisiveness)
    if outcomes_n < 3:
        width += 0.04
    elif outcomes_n < 8:
        width += 0.02
    return width


def _confidence(width: float) -> str:
    if width <= 0.10:
        return "high"
    if width >= 0.18:
        return "low"
    return "medium"


def _horizon(module: dict) -> str:
    if float(module.get("incident_raw") or 0) >= 0.4:
        return "already inside the incident window"
    if float(module.get("churn") or 0) >= 80:
        return "inside the current churn window"
    return "later in the history window"


def _expected_incidents(code: list[dict]) -> float:
    by_service: dict[str, float] = {}
    for module in code:
        service = module["service"]
        score = float(module["risks"]["incident"])
        by_service[service] = max(by_service.get(service, 0.0), score)
    return round(sum(by_service.values()), 4)


def _headline(bugs: float, incidents: float, band: dict, lead: dict | None) -> str:
    text = (
        f"Forecast about {bugs:.1f} bug-bearing modules "
        f"(p50 {band.get('bug_p50', 0)}, p90 {band.get('bug_p90', 0)}) "
        f"and {incidents:.1f} incident-bearing services."
    )
    if lead is None:
        return text
    return text + f" Sharpest call: {lead['module']} as a {lead['mode_label']}."


def apply_agreed(forecast: dict, checks: list[dict]) -> dict:
    """Shrink a module's band when a check agreed with that module's forecast mode.

    The base width stays on the row, so applying this twice does not shrink twice.
    """
    by_module = dict(forecast.get("by_module") or {})
    counts: dict[str, int] = {}
    for check in checks:
        if check.get("verdict") != "agreed":
            continue
        row = by_module.get(check.get("module"))
        if row is None or check.get("kind") != row.get("mode"):
            continue
        counts[check["module"]] = counts.get(check["module"], 0) + 1
    updated = {}
    for path, row in by_module.items():
        agreed = counts.get(path, 0)
        base = float(row.get("base_width", row.get("width") or 0))
        width = max(0.04, base - 0.03 * min(agreed, 3))
        score = float(row["score"])
        low = round(max(0.0, score - width), 4)
        high = round(min(1.0, score + width), 4)
        confidence = _confidence(width)
        revised = dict(row)
        revised.update(
            {
                "width": round(width, 4),
                "base_width": round(base, 4),
                "low": low,
                "high": high,
                "confidence": confidence,
                "agreed_checks": agreed,
            }
        )
        horizon = row.get("horizon") or ""
        revised["statement"] = (
            f"{path} is forecast as a {row.get('mode_label')} at {score:.2f} "
            f"(band {low:.2f}–{high:.2f}, {confidence} confidence)"
            + (f", {horizon}." if horizon else ".")
        )
        updated[path] = revised
    failures = [updated.get(item["module"], item) for item in forecast.get("failures") or []]
    widths = [float(row["width"]) for row in updated.values()]
    confidence = dict(forecast.get("confidence") or {})
    note = confidence.get("note") or ""
    if counts and "Agreed checks" not in note:
        note = f"{note} Agreed checks narrow the band on the module they match.".strip()
    confidence["note"] = note
    confidence["mean_width"] = round(sum(widths) / len(widths), 4) if widths else 0.0
    revised_forecast = dict(forecast)
    revised_forecast["by_module"] = updated
    revised_forecast["failures"] = failures
    revised_forecast["confidence"] = confidence
    return revised_forecast


def _confidence_note(outcomes_n: int) -> str:
    if outcomes_n <= 0:
        return (
            "No outcomes have been recorded, so every band is widened. "
            "A band is also wider when history is thin or the index sits near one half."
        )
    if outcomes_n < 8:
        return (
            f"{outcomes_n} recorded outcomes. Bands stay wider until more forecasts are confirmed or rejected."
        )
    return f"{outcomes_n} recorded outcomes narrow the bands. Thin history still widens a single call."
