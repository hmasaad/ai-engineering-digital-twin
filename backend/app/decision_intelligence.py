"""What action the existing decision allows.

The Pareto front, the value of information, and the plan are already computed.
These steps read them. A hold from accuracy blocks the balanced choice. The
learning rate is not updated, and the repository is not edited.
"""

from __future__ import annotations

from app.optimize import ALPHA, situation


def advise(view: dict, decision: dict) -> dict:
    objectives = _objectives(decision)
    causal = _causal(view, decision)
    information = _information(decision)
    planning = _planning(decision)
    allocation = _allocate(decision, planning)
    constrained = _constrain(view, decision, planning)
    bandit = _bandit(view, decision)
    learning = _learning(view, decision)
    attribution = _attribute(view, decision)
    evaluation = _evaluate(view, decision, constrained)
    return {
        "action": constrained["action"],
        "steps": [
            objectives,
            causal,
            information,
            planning,
            allocation,
            constrained["step"],
            bandit,
            learning,
            attribution,
            evaluation,
        ],
        "writes_repository": False,
        "note": (
            "These steps read the decision already computed. "
            "They do not change the Pareto front, the value of information, the plan, the learning rate, or the accuracy trust. "
            "The action the constraints allow is named above. "
            "An index is not a calibrated probability. The repository is not edited."
        ),
    }


def _objectives(decision: dict) -> dict:
    front = list(decision.get("pareto") or [])
    dominated = list(decision.get("dominated") or [])
    chosen = decision.get("recommendation") or {}
    title = chosen.get("title") or "Leave it as it is"
    front_word = "action stays" if len(front) == 1 else "actions stay"
    dominated_word = "is dominated" if len(dominated) == 1 else "are dominated"
    rows = [
        {
            "title": item.get("title") or item.get("action") or "",
            "detail": f"balanced value {float(item.get('value') or 0):.2f} · bug {float(item.get('bug') or 0):.2f}",
        }
        for item in front
    ]
    return _step(
        1,
        "Multi-Objective Engineering Optimization",
        (
            f"The balanced choice is {title}. {len(front)} {front_word} on the Pareto front"
            f" and {len(dominated)} {dominated_word}. Objectives stay separate and are not folded into the regret score."
        ),
        rows,
        action=chosen.get("action"),
    )


def _causal(view: dict, decision: dict) -> dict:
    rows = []
    for item in decision.get("causal") or []:
        rows.append(
            {
                "title": item.get("title") or item.get("action") or "",
                "detail": f"{item.get('intervention') or 'do(action)'} · assumption until a recorded outcome supports it",
            }
        )
    outcomes = _outcomes(view, decision.get("module"))
    support = "A recorded outcome is on this file." if outcomes else "No recorded outcome on this file supports the mechanism yet."
    return _step(
        2,
        "Causal Decision Intelligence",
        (
            "do(action) is the intervention already written for the actions on the front. "
            f"{support} The bug index is not resimulated."
        ),
        rows,
    )


def _information(decision: dict) -> dict:
    info = decision.get("information") or {}
    value = round(float(info.get("value") or 0), 4)
    rows = [
        {
            "title": probe.get("question") or "",
            "detail": (
                f"cost {float(probe.get('loss') or 0):.2f} · would choose {probe.get('title')}"
                if probe.get("changes_decision")
                else "does not change the balanced choice"
            ),
        }
        for probe in decision.get("probes") or []
    ]
    return _step(
        3,
        "Value of Information (VoI)",
        (
            f"The value of the leading question is {value:.2f}. "
            f"{info.get('note') or ''} "
            "This value is the cost of committing under the wrong objective. It is not a probability."
        ).strip(),
        rows,
        value=value,
    )


def _planning(decision: dict) -> dict:
    plan = decision.get("plan") or {}
    labels = (("now", "Now"), ("if_failure", "If the failure lands"), ("if_holds", "If it holds"))
    rows = []
    for key, label in labels:
        step = plan.get(key) or {}
        rows.append({"title": f"{label}: {step.get('title') or ''}", "detail": step.get("because") or ""})
    now = plan.get("now") or {}
    current = str(now.get("title") or "undecided").rstrip(".")
    return _step(
        4,
        "Decision-Theoretic Planning",
        f"The plan's current step is {current}. The next branch depends on whether the failure lands.",
        rows,
        action=now.get("action"),
    )


def _allocate(decision: dict, planning: dict) -> dict:
    module = decision.get("module") or "this file"
    now = (decision.get("plan") or {}).get("now") or {}
    front = decision.get("pareto") or []
    listed = "action" if len(front) == 1 else "actions"
    allocated = str(now.get("title") or "the current plan").rstrip(".")
    return _step(
        5,
        "Risk-Aware Resource Allocation",
        (
            f"One step is allocated on {module}: {allocated}. "
            f"The front lists {len(front)} {listed}. They are not stacked."
        ),
        [{"title": now.get("title") or "", "detail": f"mode {decision.get('mode') or 'bug'} · effect {float(decision.get('gain') or 0):.2f} · band {float(decision.get('width') or 0):.2f}"}],
        action=planning.get("action"),
    )


def _constrain(view: dict, decision: dict, planning: dict) -> dict:
    trust = (view.get("accuracy") or {}).get("trust") or "hold"
    chosen = (decision.get("recommendation") or {}).get("title") or "the balanced choice"
    now = (decision.get("plan") or {}).get("now") or {}
    if trust == "hold":
        action = {
            "name": "hold",
            "title": "Hold the balanced choice",
            "because": (
                f"The balanced choice is {chosen}. Accuracy trust is hold, so it stays blocked. "
                "The repository is not edited."
            ),
        }
        summary = action["because"]
    else:
        action = {
            "name": now.get("action") or "measure",
            "title": now.get("title") or "Measure",
            "because": now.get("because") or "The plan's current step is the allowed action.",
        }
        summary = (
            f"Accuracy trust is {trust}. The allowed step is {action['title']}. The repository is not edited."
        )
    step = _step(
        6,
        "Constrained Decision Optimization",
        summary,
        [{"title": action["title"], "detail": f"trust {trust} · repository stays as it is"}],
        action=action["name"],
    )
    return {"action": action, "step": step}


def _bandit(view: dict, decision: dict) -> dict:
    module_path = decision.get("module") or ""
    module = next((item for item in view.get("modules") or [] if item.get("path") == module_path), None)
    context = situation(module) if module else "quiet"
    chosen = decision.get("recommendation") or {}
    arm = chosen.get("title") or chosen.get("action") or "the balanced choice"
    loop = view.get("loop") or {}
    visits = 0
    if loop.get("module") == module_path:
        match = next((item for item in loop.get("actions") or [] if item.get("action") == chosen.get("action")), None)
        if match:
            visits = int(match.get("visits") or 0)
        where = "The visits are on this file."
    elif loop.get("module"):
        where = f"The stored policy is for {loop.get('module')}, so this file does not draw from that table."
    else:
        where = "No policy table is stored."
    return _step(
        7,
        "Contextual Bandits for Engineering Decisions",
        (
            f"Context is {context}. The arm stays {arm}, already chosen by the balanced objectives. "
            f"Recorded visits: {visits}. {where} This step does not draw a new arm."
        ),
        [{"title": arm, "detail": f"context {context} · visits {visits}"}],
        action=chosen.get("action"),
    )


def _learning(view: dict, decision: dict) -> dict:
    loop = view.get("loop") or {}
    module = decision.get("module") or ""
    recorded = []
    if loop.get("module") == module:
        recorded = list(loop.get("steps") or [])
    if recorded:
        last = recorded[0]
        detail = f"last reward {float(last.get('reward') or 0):.2f} on {last.get('action') or 'the action'}"
    else:
        detail = "No recorded step on this file."
    return _step(
        8,
        "Reinforcement Learning for Adaptive Decisions",
        f"The learning rate stays {ALPHA:.2f}. This step does not update the action value. {detail}",
        [{"title": "Learning rate", "detail": f"{ALPHA:.2f} · not updated"}],
        updated=False,
        alpha=ALPHA,
    )


def _attribute(view: dict, decision: dict) -> dict:
    outcomes = _outcomes(view, decision.get("module"))
    if not outcomes:
        return _step(
            9,
            "Decision Outcome Attribution",
            "No outcome is recorded on this file. The decision is not attributed to a result.",
            [],
            reading="unvalidated",
        )
    gap = sum(float(item.get("predicted") or 0) - (1.0 if item.get("happened") else 0.0) for item in outcomes) / len(outcomes)
    return _step(
        9,
        "Decision Outcome Attribution",
        (
            f"{len(outcomes)} outcomes are recorded on this file. The mean gap is {gap:.2f}. "
            "The decision index is not moved."
        ),
        [],
        reading="compared",
    )


def _evaluate(view: dict, decision: dict, constrained: dict) -> dict:
    accuracy = view.get("accuracy") or {}
    trust = accuracy.get("trust") or "hold"
    reading = accuracy.get("reading") or "uncalibrated"
    allowed = (constrained.get("action") or {}).get("title") or "Hold the balanced choice"
    return _step(
        10,
        "Continuous Decision Policy Evaluation",
        (
            f"Policy evaluation reads trust {trust} / {reading}. It does not change that trust. "
            f"The action the constraints allow is {allowed}."
        ),
        [{"title": allowed, "detail": f"trust {trust} · {reading}"}],
        trust=trust,
        reading=reading,
    )


def _outcomes(view: dict, module: str | None) -> list[dict]:
    return [item for item in view.get("outcomes") or [] if item.get("module") == module]


def _step(order: int, title: str, summary: str, rows: list[dict], **extra) -> dict:
    return {"order": order, "title": title, "summary": summary, "rows": rows, **extra}
