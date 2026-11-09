"""One autonomous run over the council plan.

Agents that share a file wait for each other. A measurement on a single file
can be recorded by the twin. Security work, high severity, a wide blast
radius, and large effort wait for a person. Nothing here edits the repository.
A rejected or failed step releases the next agent with a measurement. The
model changes only when a person records the outcome.
"""

from __future__ import annotations

RULES = [
    "The twin never writes the repository.",
    "Agents that share a file run in council order. The later agent waits.",
    "A measurement on one file, below high severity, runs on its own.",
    "Security work, high or critical severity, a wide blast radius, and large effort wait for a person.",
    "A rejected or failed step releases the next agent with a measurement, not the original change.",
    "The model updates only after a person records whether the outcome happened.",
]

_AGENT_ACTION = {
    "Security": ("hold", "bug"),
    "Reliability": ("add_circuit_breaker", "incident"),
    "Performance": ("measure", "perf"),
    "Quality": ("add_tests", "bug"),
    "Architect": ("extract_module", "bug"),
    "Delivery": ("add_owner", "delay"),
}

ACTION_TITLE = {
    "hold": "Hold for a person",
    "add_circuit_breaker": "Add a circuit breaker",
    "measure": "Record the outcome",
    "add_tests": "Add characterization tests",
    "extract_module": "Extract a module",
    "add_owner": "Add a second owner",
}

_OPEN = {"waiting", "pending", "awaiting"}


def idle_mission() -> dict:
    return {
        "status": "idle",
        "recovered": False,
        "writes_repository": False,
        "rules": list(RULES),
        "tasks": [],
        "log": [],
    }


def build_mission(plan: list[dict]) -> dict:
    """Turn the sequenced council plan into a gated run."""
    tasks: list[dict] = []
    last_on: dict[str, str] = {}
    for index, step in enumerate(plan[:8], start=1):
        modules = [module for module in step.get("modules") or [] if module]
        action, kind = _AGENT_ACTION.get(step["agent"], ("measure", "bug"))
        safety, gate = _classify(step["agent"], step["severity"], modules, step.get("effort") or "M")
        task_id = f"t{index}"
        shared = next((module for module in modules if module in last_on), None)
        waits_on = last_on[shared] if shared else None
        prior = next((item for item in tasks if item["id"] == waits_on), None) if waits_on else None
        task = {
            "id": task_id,
            "agent": step["agent"],
            "severity": step["severity"],
            "title": step["title"],
            "remediation": step.get("remediation") or "",
            "modules": modules,
            "action": action,
            "action_title": ACTION_TITLE.get(action, action),
            "kind": kind,
            "safety": safety,
            "gate": gate,
            "waits_on": waits_on,
            "coordination": _coordination(step["agent"], prior, shared, modules),
            "status": "pending",
            "approver": None,
            "repository": "unchanged",
            "result": "",
            "learned": False,
            "outcome": None,
            "recovered": False,
        }
        for module in modules:
            last_on[module] = task_id
        tasks.append(task)
    for task in tasks:
        if task["waits_on"]:
            task["status"] = "waiting"
        else:
            _place(task)
    for task in list(tasks):
        if task["status"] == "done":
            _release(tasks, task["id"])
    mission = idle_mission()
    mission["tasks"] = tasks
    mission["status"] = _status(tasks, False)
    _stamp(mission, "started", "twin", "Started the run. Safe measurements were recorded. Everything else waits.")
    return mission


def decide(mission: dict, task_id: str, approve: bool) -> dict:
    task = _require(mission, task_id)
    if approve:
        if task["status"] != "awaiting":
            raise KeyError("That step is not waiting for approval")
        task["status"] = "done"
        task["approver"] = "human"
        task["result"] = "Approved as intent. The repository was not edited."
        _release(mission["tasks"], task_id)
        _stamp(mission, "approved", "human", f"Approved {task['agent']} on {task['title']}.")
    else:
        if task["status"] not in {"awaiting", "blocked"}:
            raise KeyError("That step is not waiting for approval")
        task["status"] = "rejected"
        task["approver"] = "human"
        task["result"] = "Rejected. The step will not run."
        note = _recover(mission, task, "rejected")
        _stamp(mission, "rejected", "human", f"Rejected {task['agent']} on {task['title']}. {note}")
    mission["status"] = _status(mission["tasks"], mission["recovered"])
    return mission


def fail(mission: dict, task_id: str) -> dict:
    task = _require(mission, task_id)
    if task["status"] != "done":
        raise KeyError("Only a completed step can fail")
    task["status"] = "failed"
    task["result"] = "The step did not hold."
    note = _recover(mission, task, "failed")
    _stamp(mission, "failed", "human", f"{task['agent']} did not hold on {task['title']}. {note}")
    mission["status"] = _status(mission["tasks"], mission["recovered"])
    return mission


def prepare_learning(mission: dict, task_id: str) -> tuple[str, str]:
    task = _require(mission, task_id)
    if task["status"] != "done":
        raise KeyError("Record an outcome after the step is done")
    if task["learned"]:
        raise KeyError("This step already updated the model")
    if len(task["modules"]) != 1:
        raise KeyError("Learning needs a single module")
    return task["modules"][0], task["kind"]


def mark_learned(mission: dict, task_id: str, happened: bool, note: str) -> dict:
    task = _require(mission, task_id)
    prepare_learning(mission, task_id)
    task["learned"] = True
    task["outcome"] = happened
    _stamp(mission, "learned", "human", note)
    mission["status"] = _status(mission["tasks"], mission["recovered"])
    return mission


def _place(task: dict) -> None:
    if task["safety"] == "measure":
        _mark_measured(task)
    elif task["modules"]:
        task["status"] = "awaiting"
    else:
        task["status"] = "blocked"


def _mark_measured(task: dict) -> None:
    task["status"] = "done"
    task["approver"] = "twin"
    task["repository"] = "unchanged"
    task["result"] = "Recorded inside the twin. The repository was not edited."


def _release(tasks: list[dict], finished_id: str) -> None:
    opened: list[str] = []
    prior = next(item for item in tasks if item["id"] == finished_id)
    for child in tasks:
        if child["waits_on"] != finished_id or child["status"] not in {"waiting", "pending"}:
            continue
        child["coordination"] = f"{child['agent']} follows {prior['agent']}. That step is done."
        _place(child)
        if child["status"] == "done":
            opened.append(child["id"])
    for task_id in opened:
        _release(tasks, task_id)


def _recover(mission: dict, task: dict, how: str) -> str:
    changed = False
    for child in mission["tasks"]:
        if child["waits_on"] != task["id"] or child["status"] not in _OPEN:
            continue
        changed = True
        child["waits_on"] = None
        child["recovered"] = True
        if child["action"] != "measure":
            child["action"] = "measure"
            child["action_title"] = ACTION_TITLE["measure"]
            child["safety"] = "measure"
            verb = "failed" if how == "failed" else "was rejected"
            child["gate"] = f"Recovered after {task['agent']} {verb}. This step only records the outcome."
        child["coordination"] = f"{child['agent']} continues with a measurement."
        if child["modules"]:
            _mark_measured(child)
            _release(mission["tasks"], child["id"])
        else:
            child["status"] = "blocked"
    if changed:
        mission["recovered"] = True
        return "Later steps continue with a measurement."
    return "No later step was waiting."


def _classify(agent: str, severity: str, modules: list[str], effort: str) -> tuple[str, str]:
    reasons = []
    if agent == "Security":
        reasons.append("Security work stays with a person.")
    if severity in {"critical", "high"}:
        reasons.append(f"{severity.capitalize()} severity waits for approval.")
    if not modules:
        reasons.append("No single file to act on.")
    elif len(modules) > 1:
        reasons.append(f"Blast radius is {len(modules)} modules.")
    if effort == "L":
        reasons.append("Large effort waits for approval.")
    if reasons:
        return "governed", " ".join(reasons)
    return "measure", "Cleared. One file, and the twin only records the measurement."


def _coordination(agent: str, prior: dict | None, shared: str | None, modules: list[str]) -> str:
    if prior is not None and shared is not None:
        return f"{agent} waits for {prior['agent']} on {_short(shared)}."
    return f"{agent} leads {_lead(modules)}."


def _lead(modules: list[str]) -> str:
    if not modules:
        return "this work"
    name = _short(modules[0])
    extra = len(modules) - 1
    if extra == 1:
        return f"{name} and 1 more"
    if extra > 1:
        return f"{name} and {extra} more"
    return name


def _short(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _status(tasks: list[dict], recovered: bool) -> str:
    if not tasks:
        return "idle"
    if any(task["status"] == "awaiting" for task in tasks):
        return "awaiting"
    if recovered:
        return "recovered"
    if any(task["status"] in {"waiting", "pending"} for task in tasks):
        return "running"
    return "settled"


def _require(mission: dict, task_id: str) -> dict:
    for task in mission["tasks"]:
        if task["id"] == task_id:
            return task
    raise KeyError("Unknown step")


def _stamp(mission: dict, event: str, actor: str, detail: str) -> None:
    mission["log"].append({"event": event, "actor": actor, "detail": detail})
