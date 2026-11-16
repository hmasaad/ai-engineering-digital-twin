"""What a run is allowed to execute.

The council plan, the agent order, and the approval gates are already on the
mission. A code change and a test stay proposals. CI is not started. The
repository is not edited, so a failed step has nothing in the tree to roll back.
"""

from __future__ import annotations


def describe(mission: dict) -> dict:
    tasks = list(mission.get("tasks") or [])
    log = list(mission.get("log") or [])
    return {
        "steps": [
            _plan(tasks),
            _orchestrate(tasks),
            _code(tasks),
            _ci(),
            _gates(tasks),
            _recover(mission, tasks),
            _audit(log),
        ],
        "writes_repository": False,
        "launches_ci": False,
        "note": (
            "The run plans the council work, keeps the human gates, and records the audit. "
            "A code change and a test stay proposals. CI is not started. "
            "The repository is not edited. A rejected or failed step has nothing in the repository to roll back."
        ),
    }


def _plan(tasks: list[dict]) -> dict:
    if not tasks:
        return _step(1, "Autonomous engineering task planning", "No run is planned yet.", [])
    first = tasks[0]
    rows = [
        {"title": f"{task.get('agent')} · {task.get('title')}", "detail": f"{task.get('action_title')} · {task.get('status')}"}
        for task in tasks
    ]
    noun = "step is" if len(tasks) == 1 else "steps are"
    return _step(
        1,
        "Autonomous engineering task planning",
        f"{len(tasks)} {noun} planned from the council. The first is {first.get('agent')} on {first.get('title')}.",
        rows,
    )


def _orchestrate(tasks: list[dict]) -> dict:
    waiting = [task for task in tasks if task.get("waits_on")]
    if not tasks:
        summary = "No agents are in this run."
    elif waiting:
        summary = f"{len(waiting)} agents wait for an earlier agent that shares a file. Council order is unchanged."
    else:
        summary = "No agent is waiting on another. Each leads its own file."
    rows = [{"title": task.get("agent") or "", "detail": task.get("coordination") or ""} for task in tasks if task.get("coordination")]
    return _step(2, "Multi-agent orchestration", summary, rows)


def _code(tasks: list[dict]) -> dict:
    prepared = next((task for task in tasks if task.get("action") == "add_tests" and task.get("modules")), None)
    if prepared is None:
        return _step(
            3,
            "Code modification and test generation",
            "No code change is prepared. The steps record a measurement or wait for a person.",
            [],
            applied=False,
        )
    module = prepared["modules"][0]
    name = module.rsplit("/", 1)[-1]
    return _step(
        3,
        "Code modification and test generation",
        f"A characterization test for {name} is prepared and not written. Approval records intent.",
        [{"title": name, "detail": "proposal only · the repository stays unchanged"}],
        applied=False,
    )


def _ci() -> dict:
    return _step(
        4,
        "CI/CD workflow execution",
        "No CI job is started. The twin does not call a pipeline, and a proposal is not a build result.",
        [{"title": "Pipeline", "detail": "not started"}],
        launched=False,
    )


def _gates(tasks: list[dict]) -> dict:
    awaiting = [task for task in tasks if task.get("status") == "awaiting"]
    if not tasks:
        summary = "No gate is open."
    elif not awaiting:
        summary = "No step is waiting for a person. A completed measurement still leaves the repository unchanged."
    else:
        noun = "step waits" if len(awaiting) == 1 else "steps wait"
        lead = awaiting[0]
        summary = (
            f"{len(awaiting)} {noun} for a person. {lead.get('agent')} on {lead.get('title')} is one of them. "
            "Approval records intent and leaves the repository unchanged."
        )
    rows = [{"title": task.get("agent") or "", "detail": task.get("gate") or ""} for task in awaiting]
    return _step(5, "Human approval gates", summary, rows)


def _recover(mission: dict, tasks: list[dict]) -> dict:
    recovered = [task for task in tasks if task.get("recovered")]
    if recovered:
        summary = (
            f"{len(recovered)} later steps continue with a measurement. "
            "Nothing was written, so rollback is that plan change inside the twin."
        )
    else:
        summary = (
            "No step has been rolled back. If one is rejected or fails, the next agent continues with a measurement. "
            "There is no repository edit to revert."
        )
    rows = [{"title": task.get("agent") or "", "detail": task.get("gate") or ""} for task in recovered]
    return _step(6, "Failure recovery and rollback", summary, rows, recovered=bool(mission.get("recovered")))


def _audit(log: list[dict]) -> dict:
    if not log:
        summary = "The audit trail starts when a run starts."
    else:
        noun = "entry is" if len(log) == 1 else "entries are"
        summary = f"{len(log)} log {noun} the audit trail."
    rows = [{"title": entry.get("actor") or "", "detail": entry.get("detail") or ""} for entry in reversed(log)]
    return _step(7, "Execution monitoring and audit trails", summary, rows)


def _step(order: int, title: str, summary: str, rows: list[dict], **extra) -> dict:
    return {"order": order, "title": title, "summary": summary, "rows": rows, **extra}
