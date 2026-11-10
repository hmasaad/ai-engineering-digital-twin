"""Predict the engineering failure a dependency points to.

The score is the dependency index that already reached the module. The mode
is a build failure when history records a failure, an upgrade, or a native
binding, and a test failure when the only signal is an undeclared import on
an untested file. It is not the bug index and it is not a probability.
"""

from __future__ import annotations

MODE_LABEL = {"build": "build failure", "test": "test failure"}


def predict(view: dict) -> dict:
    dependencies = view.get("dependencies") or {}
    packages = {item["name"]: item for item in dependencies.get("packages") or []}
    modules = {item["path"]: item for item in view.get("modules") or []}
    rows = []
    for landed in dependencies.get("modules") or []:
        package = packages.get(landed.get("via") or "")
        if package is None:
            continue
        keys = {signal["key"] for signal in package.get("signals") or []}
        module = modules.get(landed["module"]) or {}
        mode = _mode(keys, float(module.get("test_gap") or 0))
        score = float(landed["risk"])
        width = _width(keys)
        low = round(max(0.0, score - width), 4)
        high = round(min(1.0, score + width), 4)
        confidence = _confidence(width)
        label = MODE_LABEL[mode]
        rows.append(
            {
                "module": landed["module"],
                "service": landed.get("service") or "",
                "package": package["name"],
                "version": package.get("version") or "",
                "mode": mode,
                "mode_label": label,
                "score": round(score, 4),
                "low": low,
                "high": high,
                "width": width,
                "depth": landed.get("depth", 0),
                "confidence": confidence,
                "drivers": list(package.get("drivers") or []),
                "statement": (
                    f"A {label} is forecast for {landed['module']} through {package['name']} "
                    f"at {score:.2f} (band {low:.2f}–{high:.2f}, {confidence} confidence)."
                ),
            }
        )
    rows.sort(key=lambda item: (-item["score"], item["depth"], item["module"]))
    lead = rows[0] if rows else None
    return {
        "failures": rows[:8],
        "headline": _headline(lead),
        "note": (
            "This index is the dependency risk that reached the file. "
            "A recorded build failure, upgrade, or native binding forecasts a build failure. "
            "An undeclared import on an untested file forecasts a test failure. "
            "It is not the bug index and it is not a probability that the next build will fail."
        ),
    }


def _mode(keys: set[str], test_gap: float) -> str:
    if keys & {"failure", "upgrade", "plugin"}:
        return "build"
    if "undeclared" in keys and test_gap >= 0.5:
        return "test"
    return "build"


def _width(keys: set[str]) -> float:
    if "failure" in keys:
        return 0.08
    if "upgrade" in keys:
        return 0.12
    return 0.16


def _confidence(width: float) -> str:
    if width <= 0.10:
        return "high"
    if width >= 0.15:
        return "low"
    return "medium"


def _headline(lead: dict | None) -> str:
    if lead is None:
        return "No engineering failure is forecast from the current dependencies."
    return (
        f"The leading forecast is a {lead['mode_label']} through {lead['package']}, "
        f"landing on {lead['module']} at {lead['score']:.2f}."
    )
