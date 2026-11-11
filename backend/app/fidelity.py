"""How closely the twin matches the repository, and what is still unproven.

Edges are imports the scanner bound to a file. Scores are associations.
A What if delta is a model intervention. None of those is evidence that a
deployment will succeed. A build, a test, or a production outcome can support
or contradict a prediction. Two of those checks, with an action between them,
are what it takes to say an action was followed by a better outcome.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.calibrate import compare
from app.forecast import apply_agreed

PRINCIPLE = (
    "A simulation is not evidence that a deployment will succeed. "
    "A prediction is supported or contradicted only by an isolated build, a test, or a production outcome."
)

ACTIONS = {"", "add_tests", "extract_module", "add_owner", "add_circuit_breaker", "freeze", "hold", "measure"}
CHANNELS = ("build", "test", "production")


def judge(predicted: float, low: float, high: float, passed: bool) -> str:
    """Compare a point score with a real check. A band that crosses one half decides nothing."""
    if low < 0.5 < high:
        return "uncertain"
    expected_fail = predicted >= 0.5
    failed = not passed
    if expected_fail == failed:
        return "agreed"
    return "false_alarm" if expected_fail else "missed"


def updates_model(check: dict) -> bool:
    """A production check trains the incident index only when the band does not cross one half."""
    return check.get("channel") == "production" and check.get("verdict") not in {"uncertain", "recorded"}


def prepare_check(view: dict, module: str, channel: str, passed: bool, action: str, note: str) -> dict:
    if channel not in CHANNELS:
        raise KeyError("Channel must be build, test, or production")
    if action not in ACTIONS:
        raise KeyError("Unknown action")
    target = next(
        (item for item in view["modules"] if item["path"] == module and item["role"] == "code"),
        None,
    )
    if target is None:
        raise KeyError(f"Unknown module: {module}")
    if channel == "production":
        return _scored(target, view, "incident", "production", passed, action, note, "recorded")
    return _scored(target, view, "bug", channel, passed, action, note, "recorded", force_recorded=True)


def history_checks(view: dict, raw: dict) -> list[dict]:
    """Deploys and incidents are production checks. They are not typed in, and they do not train by themselves."""
    by_service: dict[str, list[dict]] = defaultdict(list)
    for module in view.get("modules") or []:
        if module.get("role") == "code":
            by_service[module["service"]].append(module)
    rows = []
    for event in raw.get("events") or []:
        service = event.get("service")
        kind = event.get("type")
        if kind not in {"deploy", "incident"} or not service or service not in by_service:
            continue
        lead = max(by_service[service], key=lambda module: float(module["risks"]["incident"]))
        result = event.get("result")
        if result is None:
            result = (event.get("data") or {}).get("result")
        passed = kind == "deploy" and result == "success"
        summary = event.get("summary") or f"{service} {kind}"
        row = _scored(lead, view, "incident", "production", passed, "", summary, "history")
        row["service"] = service
        row["created_at"] = _observed_at(raw, event.get("age_days", 0))
        rows.append(row)
    rows.sort(key=lambda item: item["created_at"])
    return rows


def assess(view: dict, raw: dict, checks: list[dict]) -> dict:
    manual = [{**check, "source": check.get("source") or "recorded"} for check in checks]
    combined = manual + history_checks(view, raw)
    combined.sort(key=lambda item: item.get("created_at") or "")
    forecast = apply_agreed(view.get("forecast") or {}, combined)
    graph = _graph(raw)
    return {
        "principle": PRINCIPLE,
        "verdict": _verdict(combined),
        "graph": graph,
        "calibration": compare(view, raw, graph),
        "uncertainty": _uncertainty(forecast),
        "learning": _learning(view.get("outcomes") or [], view.get("weight_history") or []),
        "causation": _causation(view, combined),
        "improvement": _improvement(combined),
        "channels": _channels(combined),
        "labels": _labels(view.get("outcomes") or []),
        "checks": list(reversed(combined[-12:])),
    }


def _graph(raw: dict) -> dict:
    modules = raw.get("modules") or []
    known = {module["path"] for module in modules}
    edges = raw.get("edges") or []
    resolved = [edge for edge in edges if edge["source"] in known and edge["target"] in known]
    dangling = len(edges) - len(resolved)
    unresolved = [
        {"module": module["path"], "spec": spec}
        for module in modules
        for spec in module.get("unresolved_imports") or []
    ]
    external = {name for module in modules for name in module.get("external_imports") or []}
    declared = set(raw.get("dependencies") or [])
    dynamic = sum(int(module.get("dynamic_imports") or 0) for module in modules)
    tracked = any("import_specs" in module for module in modules)
    if not tracked:
        coverage = None
        note = (
            "Rescan the folder before reading coverage. "
            "Edge count alone is the imports that already bound, so it cannot show the ones the scanner dropped. "
            "Declared packages are manifest names. The twin does not install them, and this is not the build graph."
        )
    else:
        denom = len(resolved) + len(unresolved) + dynamic
        coverage = 1.0 if denom == 0 else len(resolved) / denom
        note = (
            "Coverage is resolved file imports divided by resolved imports, unbound imports, and dynamic imports. "
            "Drawn edges alone are not the denominator. "
            "Declared packages are manifest names. The twin does not install them, and this is not the build graph."
        )
    return {
        "edges": len(edges),
        "resolved": len(resolved),
        "dangling": dangling,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved[:8],
        "dynamic_count": dynamic,
        "external_count": len(external),
        "undeclared_count": len(external - declared),
        "undeclared": sorted(external - declared)[:8],
        "unused_count": len(declared - external),
        "unused": sorted(declared - external)[:8],
        "coverage": None if coverage is None else round(coverage, 4),
        "note": note,
    }


def _uncertainty(forecast: dict) -> dict:
    rows = list((forecast.get("by_module") or {}).values())
    counts = {"high": 0, "medium": 0, "low": 0}
    for row in rows:
        counts[row.get("confidence") or "low"] = counts.get(row.get("confidence") or "low", 0) + 1
    widest = max(rows, key=lambda row: float(row.get("width") or 0), default=None)
    return {
        "calls": len(rows),
        "high": counts.get("high", 0),
        "medium": counts.get("medium", 0),
        "low": counts.get("low", 0),
        "widest": None
        if widest is None
        else {
            "module": widest["module"],
            "width": widest["width"],
            "confidence": widest["confidence"],
            "low": widest["low"],
            "high": widest["high"],
        },
        "note": (
            "The band is how unsure the twin is. It is not a probability that a deploy will fail. "
            "A check inside a band that crosses 0.50 does not confirm the point score."
        ),
    }


def _learning(outcomes: list[dict], history: list[dict]) -> dict:
    wrong = []
    for outcome in outcomes:
        predicted = float(outcome.get("predicted") or 0)
        happened = bool(outcome.get("happened"))
        called = predicted >= 0.5
        if called != happened:
            wrong.append(
                {
                    "module": outcome.get("module"),
                    "kind": outcome.get("kind"),
                    "predicted": round(predicted, 4),
                    "happened": happened,
                }
            )
    latest = history[0] if history else None
    moved = []
    if latest:
        for delta in (latest.get("deltas") or [])[:3]:
            moved.append(
                {
                    "label": delta.get("label") or delta.get("feature"),
                    "before": delta.get("before"),
                    "after": delta.get("after"),
                }
            )
    return {
        "outcomes": len(outcomes),
        "wrong": len(wrong),
        "misses": wrong[:6],
        "latest_note": None if latest is None else latest.get("note"),
        "moved": moved,
        "note": (
            "Only a production check whose band does not cross 0.50 updates the incident index. "
            "A build, a test, or an uncertain check is kept and does not move the weights. "
            "The index is still not a calibrated probability."
        ),
    }


def _causation(view: dict, checks: list[dict]) -> dict:
    bugs = [risk for risk in view.get("risks") or [] if risk.get("kind") == "bug"]
    drivers = (bugs[0].get("drivers") or [])[:3] if bugs else []
    actions = sorted({check["action"] for check in checks if check.get("action")})
    return {
        "association": (
            "Churn, missing tests, ownership, and incidents move the index when they are observed together. "
            "That is correlation."
        ),
        "drivers": drivers,
        "driver_module": bugs[0]["module"] if bugs else None,
        "intervention": (
            "What if and Decide apply do(action) inside the model. "
            "The delta is not a measured cause, and it is not evidence the deploy will succeed."
        ),
        "standard": (
            "An action counts only when it sits between a failed check and a later pass on the same file. "
            "A file that failed on both sides of that window and had no action is the comparison. "
            "Neither one proves the action caused the change."
        ),
        "checked_actions": actions,
    }


def _improvement(checks: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for check in sorted(checks, key=lambda item: item.get("created_at") or ""):
        grouped[check["module"]].append(check)
    rows = []
    for module, group in grouped.items():
        window = _window(group)
        acted = [item["action"] for item in group if item.get("action")]
        if window is None:
            if not acted:
                continue
            rows.append(
                {
                    "module": module,
                    "action": acted[-1],
                    "status": "unproven",
                    "detail": "The action does not sit between a failed check and a later pass.",
                    "checks": len(group),
                }
            )
            continue
        failure, later = window
        between = [
            item["action"]
            for item in group
            if item.get("action") and failure["created_at"] < item.get("created_at", "") < later["created_at"]
        ]
        if not between:
            rows.append(
                {
                    "module": module,
                    "action": "",
                    "status": "unproven",
                    "detail": (
                        f"A later {later['channel']} passed after an earlier failure. "
                        "No recommended action sits between those checks."
                    ),
                    "checks": len(group),
                }
            )
            continue
        other = _untreated(grouped, module, failure["created_at"], later["created_at"])
        comparison = (
            f" {other} failed on both sides of this window and had no action. That is a comparison, not a controlled trial."
            if other
            else " No other file is held out as an untreated comparison."
        )
        rows.append(
            {
                "module": module,
                "action": between[-1],
                "status": "consistent",
                "detail": (
                    f"A later {later['channel']} passed after {between[-1]}, which sits between the failure and the pass. "
                    "That is consistent with improvement. It is not proof the action caused it."
                    + comparison
                ),
                "checks": len(group),
            }
        )
    rows.sort(key=lambda row: row["module"])
    return {
        "rows": rows[:8],
        "note": (
            "An action counts only when a failure comes before it and a pass comes after it on the same file. "
            "A simulation delta is not one of those checks."
        ),
    }


def _channels(checks: list[dict]) -> dict:
    report = {}
    for channel in CHANNELS:
        rows = [check for check in checks if check["channel"] == channel]
        report[channel] = {
            "n": len(rows),
            "agreed": sum(1 for check in rows if check["verdict"] == "agreed"),
            "contradicted": sum(1 for check in rows if check["verdict"] in {"missed", "false_alarm"}),
            "uncertain": sum(1 for check in rows if check["verdict"] == "uncertain"),
            "recorded": sum(1 for check in rows if check["verdict"] == "recorded"),
        }
    return report


def _labels(outcomes: list[dict]) -> dict:
    return {
        "n": len(outcomes),
        "note": "A label recorded in Learning updates the model. It is not an isolated build, a test, or a production outcome.",
    }


def _scored(
    target: dict,
    view: dict,
    index: str,
    channel: str,
    passed: bool,
    action: str,
    note: str,
    source: str,
    force_recorded: bool = False,
) -> dict:
    score = float(target["risks"][index])
    band = ((view.get("forecast") or {}).get("by_module") or {}).get(target["path"]) or {}
    width = float(band.get("base_width", band.get("width", 0.12)) or 0.12)
    low = round(max(0.0, score - width), 4)
    high = round(min(1.0, score + width), 4)
    verdict = "recorded" if force_recorded else judge(score, low, high, bool(passed))
    return {
        "module": target["path"],
        "channel": channel,
        "kind": "incident" if channel == "production" else channel,
        "passed": bool(passed),
        "predicted": round(score, 4),
        "low": low,
        "high": high,
        "action": action,
        "note": (note or "").strip(),
        "verdict": verdict,
        "source": source,
        "created_at": "",
    }


def _observed_at(raw: dict, age_days: float) -> str:
    age = float(age_days or 0)
    text = raw.get("scanned_at")
    if text:
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (moment - timedelta(days=age)).isoformat()
    return f"{100000 - age:012.4f}"


def _window(group: list[dict]) -> tuple[dict, dict] | None:
    failure = next((item for item in group if not item["passed"]), None)
    if failure is None:
        return None
    later = next(
        (
            item
            for item in group
            if item.get("created_at", "") > failure.get("created_at", "") and item["passed"]
        ),
        None,
    )
    if later is None:
        return None
    return failure, later


def _untreated(grouped: dict[str, list[dict]], module: str, start: str, end: str) -> str:
    for other, group in grouped.items():
        if other == module:
            continue
        failed_before = any(not item["passed"] and item.get("created_at", "") <= start for item in group)
        failed_after = any(not item["passed"] and item.get("created_at", "") >= end for item in group)
        acted = any(item.get("action") and start < item.get("created_at", "") < end for item in group)
        if failed_before and failed_after and not acted:
            return other.rsplit("/", 1)[-1]
    return ""


def _verdict(checks: list[dict]) -> str:
    if not checks:
        return "unvalidated"
    if any(check["verdict"] in {"missed", "false_alarm"} for check in checks):
        return "contradicted"
    firm = [check for check in checks if check["verdict"] == "agreed"]
    if not firm:
        return "unvalidated"
    if any(check["channel"] == "production" and check["verdict"] == "agreed" for check in checks):
        return "supported"
    return "partial"
