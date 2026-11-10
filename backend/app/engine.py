"""Autonomous engineering decision.

The engine picks one engineering action from the failure forecast and the
counterfactual, then the accuracy evaluation gates it. Hold blocks the
choice. Measure asks for a result. Ready lets the choice stand. The
repository is not edited.
"""

from __future__ import annotations


def decide(view: dict, accuracy: dict) -> dict:
    proposal = _proposal(view)
    trust = accuracy.get("trust") or "hold"
    if proposal["action"] == "none":
        status = "idle"
        action = "none"
        because = proposal["because"]
    elif trust == "hold":
        status = "hold"
        action = "hold"
        because = (
            f"The engineering choice is “{proposal['title']}”. "
            f"The accuracy evaluation blocks it. {accuracy.get('summary') or ''}"
        ).strip()
    elif trust == "measure" or proposal["action"] == "measure":
        status = "measure"
        action = "measure"
        because = f"{proposal['because']} {accuracy.get('summary') or ''}".strip()
    else:
        status = "act"
        action = proposal["action"]
        because = proposal["because"]
    return {
        "status": status,
        "action": action,
        "trust": trust,
        "proposal": proposal,
        "because": because,
        "writes_repository": False,
        "note": (
            "The engine recommends one engineering action and does not edit the repository. "
            "A hold means the accuracy evaluation blocked that choice."
        ),
    }


def _proposal(view: dict) -> dict:
    failures = (view.get("engineering_failures") or {}).get("failures") or []
    if not failures:
        return _choice("none", "Leave it as it is", "", "", "No engineering failure is forecast.")
    lead = failures[0]
    module = lead.get("module") or ""
    package = lead.get("package") or ""
    upgrade = _upgrade_delta(view, package)
    if lead.get("mode") == "build" and upgrade is not None and upgrade <= -0.2:
        return _choice(
            "withhold_upgrade",
            f"Withhold the {package} upgrade",
            module,
            package,
            f"Withholding the {package} upgrade lowers its dependency index by {abs(upgrade):.2f}.",
        )
    if lead.get("mode") == "test":
        return _choice(
            "add_tests",
            f"Add tests for {module}",
            module,
            package,
            f"A test failure is forecast through {package}, and no test covers {module}.",
        )
    return _choice(
        "measure",
        f"Record whether the build fails on {module}",
        module,
        package,
        "The forecast is a build failure, and withholding an upgrade does not move the index enough to act.",
    )


def _upgrade_delta(view: dict, package: str) -> float | None:
    for case in (view.get("counterfactual") or {}).get("cases") or []:
        if case.get("package") != package:
            continue
        for world in case.get("worlds") or []:
            if world.get("id") == "withhold_upgrade":
                return float(world.get("delta") or 0.0)
    return None


def _choice(action: str, title: str, module: str, package: str, because: str) -> dict:
    return {"action": action, "title": title, "module": module, "package": package, "because": because}
