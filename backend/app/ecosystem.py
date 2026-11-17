"""What the recorded engineering loop shows, and whether a change should be kept.

Ten signals are read from history already on the twin. A slow review can
propose one workflow change. That change is not applied, and it is not adopted
until an outcome of the proposal itself is recorded. The learning rate stays
where it is.
"""

from __future__ import annotations

from app.optimize import ALPHA


def observe(view: dict) -> dict:
    signals = [
        _reviews(view),
        _rejections(view),
        _builds(view),
        _tests(),
        _regressions(),
        _deploys(view),
        _recovery(view),
        _debt(view),
        _workload(view),
        _agents(view),
    ]
    found = _identify(signals)
    proposal = _propose(found)
    experiment = _experiment(proposal)
    measured = _measure(view, proposal)
    adoption = _adopt(measured)
    return {
        "signals": signals,
        "inefficiency": found,
        "proposal": proposal,
        "experiment": experiment,
        "outcome": measured,
        "adoption": adoption,
        "summary": _summary(found, proposal, adoption),
        "writes_repository": False,
        "adapts_workflow": False,
        "note": (
            "These signals are counts and durations from recorded history. "
            "A missing history stays unmeasured. "
            "The proposal is not applied, and adoption waits for an outcome of that proposal. "
            f"The learning rate stays {ALPHA:.2f}. "
            "The repository is not edited."
        ),
    }


def _reviews(view: dict) -> dict:
    ranked = sorted(
        (
            (float(module.get("review_hours") or 0), module["path"])
            for module in view.get("modules") or []
            if module.get("role") == "code" and float(module.get("review_hours") or 0) > 0
        ),
        reverse=True,
    )
    if not ranked:
        return _signal("review", "PR review duration", "unmeasured", "No review duration is recorded.")
    hours, path = ranked[0]
    name = path.rsplit("/", 1)[-1]
    return _signal(
        "review",
        "PR review duration",
        "observed",
        f"The longest recorded review is {hours:.0f} hours on {name}.",
        value=round(hours, 2),
        module=path,
    )


def _rejections(view: dict) -> dict:
    subjects = {
        str(fact.get("subject"))
        for fact in _facts(view)
        if fact.get("predicate") == "modifies" and str(fact.get("subject") or "").startswith("pr:")
    }
    if not subjects:
        return _signal("rejection", "Code review rejection rates", "unmeasured", "No pull request is recorded.")
    count = len(subjects)
    noun = "pull request is" if count == 1 else "pull requests are"
    return _signal(
        "rejection",
        "Code review rejection rates",
        "unmeasured",
        f"{count} {noun} recorded. No rejected review is recorded, so a rejection rate is not computed.",
    )


def _builds(view: dict) -> dict:
    failures = [
        fact
        for fact in _facts(view)
        if fact.get("predicate") == "failed" and fact.get("kind") == "event"
    ]
    if not failures:
        return _signal("builds", "Build failure frequency", "unmeasured", "No build result is recorded.")
    packages = []
    for fact in _facts(view):
        if fact.get("predicate") == "attributed_to" and fact.get("kind") == "event":
            name = str(fact.get("object") or "")
            if name and name not in packages:
                packages.append(name)
    named = ", ".join(packages[:2]) or "the recorded package"
    noun = "build failure is" if len(failures) == 1 else "build failures are"
    return _signal(
        "builds",
        "Build failure frequency",
        "observed",
        (
            f"{len(failures)} {noun} recorded for {named}. "
            "No successful build is recorded, so this is not a frequency. The failure is evidence for the forecast."
        ),
    )


def _tests() -> dict:
    return _signal("tests", "Test execution time", "unmeasured", "No test duration is recorded.")


def _regressions() -> dict:
    return _signal("regressions", "Regression rates", "unmeasured", "No regression is recorded.")


def _deploys(view: dict) -> dict:
    rows = [
        fact
        for fact in _facts(view)
        if fact.get("predicate") == "deployed" and fact.get("kind") == "event"
    ]
    if not rows:
        return _signal("deploys", "Deployment frequency", "unmeasured", "No deploy is recorded.")
    rollbacks = sum(1 for fact in rows if fact.get("object") == "rollback")
    noun = "deploy is" if len(rows) == 1 else "deploys are"
    return _signal(
        "deploys",
        "Deployment frequency",
        "observed",
        f"{len(rows)} {noun} recorded, {rollbacks} of them rollbacks.",
        value=len(rows),
    )


def _recovery(view: dict) -> dict:
    incidents = [
        (str(fact.get("object") or ""), float(fact.get("at_age") or 0))
        for fact in _facts(view)
        if fact.get("predicate") == "incident_on" and fact.get("kind") == "event"
    ]
    successes = [
        (str(fact.get("subject") or ""), float(fact.get("at_age") or 0))
        for fact in _facts(view)
        if fact.get("predicate") == "deployed" and fact.get("object") == "success"
    ]
    gaps = []
    for service, age in incidents:
        later = [moment for name, moment in successes if name == service and moment < age]
        if later:
            gaps.append((age - max(later), service))
    if not gaps:
        if not incidents:
            detail = "No incident is recorded, so recovery time is unmeasured."
        else:
            detail = "An incident is recorded and no later successful deploy is recorded on that service."
        return _signal("recovery", "Incident recovery time", "unmeasured", detail)
    days, service = max(gaps)
    return _signal(
        "recovery",
        "Incident recovery time",
        "observed",
        f"The next recorded successful deploy on {service} is {days:.0f} days after the incident.",
        value=round(days, 2),
    )


def _debt(view: dict) -> dict:
    code = [module for module in view.get("modules") or [] if module.get("role") == "code"]
    if not code:
        return _signal("debt", "Technical debt growth", "unmeasured", "No module is scored.")
    mean = sum(float(module.get("debt") or 0) for module in code) / len(code)
    return _signal(
        "debt",
        "Technical debt growth",
        "unmeasured",
        f"The mean debt index is {mean:.2f}. No earlier snapshot is recorded, so growth is unmeasured.",
    )


def _workload(view: dict) -> dict:
    commits: dict[tuple, str] = {}
    for fact in _facts(view):
        if fact.get("predicate") != "changed" or fact.get("kind") != "event":
            continue
        author = str(fact.get("subject") or "unknown")
        key = (author, fact.get("at_age"), fact.get("summary"))
        commits[key] = author
    if not commits:
        return _signal("workload", "Developer workload", "unmeasured", "No commit is recorded.")
    counts: dict[str, int] = {}
    for author in commits.values():
        counts[author] = counts.get(author, 0) + 1
    name, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    return _signal(
        "workload",
        "Developer workload",
        "observed",
        f"{name} has {count} of {len(commits)} recorded commits.",
        value=count,
    )


def _agents(view: dict) -> dict:
    steps = list((view.get("loop") or {}).get("steps") or [])
    if not steps:
        return _signal(
            "agents",
            "AI agent success and failure rates",
            "unmeasured",
            "No agent outcome is recorded on the policy.",
        )
    failed = sum(1 for step in steps if step.get("happened"))
    return _signal(
        "agents",
        "AI agent success and failure rates",
        "observed",
        f"{len(steps)} policy steps are recorded and {failed} of them met a failure. This does not change the learning rate.",
        value=len(steps),
    )


def _identify(signals: list[dict]) -> dict | None:
    review = next(item for item in signals if item["id"] == "review")
    if review["reading"] == "observed" and float(review.get("value") or 0) >= 48:
        return {
            "signal": "review",
            "module": review.get("module"),
            "detail": review["detail"],
        }
    return None


def _propose(found: dict | None) -> dict:
    if not found:
        return {
            "signal": None,
            "title": "No workflow change is proposed",
            "module": None,
            "applied": False,
            "detail": "The recorded signals do not show a review that ran 48 hours or longer.",
        }
    name = str(found.get("module") or "the file").rsplit("/", 1)[-1]
    return {
        "signal": "review",
        "title": f"Keep the next review of {name} inside two days",
        "module": found.get("module"),
        "applied": False,
        "detail": f"{found['detail']} One owner would take that review. The workflow is not changed.",
    }


def _experiment(proposal: dict) -> dict:
    if not proposal.get("signal"):
        return {"status": "not_run", "detail": "No proposal is waiting to be tried."}
    return {
        "status": "not_run",
        "detail": "The proposal is not applied to the workflow. There is no before-and-after to test.",
    }


def _measure(view: dict, proposal: dict) -> dict:
    module = proposal.get("module")
    outcomes = [item for item in view.get("outcomes") or [] if item.get("module") == module]
    if not module or not outcomes:
        return {"reading": "unmeasured", "detail": "No outcome is recorded for this proposal."}
    return {
        "reading": "compared",
        "detail": f"{len(outcomes)} outcomes are recorded on the file. They are not an outcome of this proposal.",
    }


def _adopt(measured: dict) -> dict:
    return {
        "decision": "hold",
        "detail": (
            "Adoption waits for a recorded outcome of this proposal. "
            f"The learning rate stays {ALPHA:.2f} and the workflow is not changed."
        ),
    }


def _summary(found: dict | None, proposal: dict, adoption: dict) -> str:
    if not found:
        return f"No review of 48 hours or longer is recorded. The adoption decision is {adoption['decision']}."
    return f"{proposal['title']}. The proposal is not applied, and the adoption decision is {adoption['decision']}."


def _signal(key: str, title: str, reading: str, detail: str, **extra) -> dict:
    return {"id": key, "title": title, "reading": reading, "detail": detail, **extra}


def _facts(view: dict) -> list[dict]:
    return list((view.get("dynamics") or {}).get("facts") or [])
