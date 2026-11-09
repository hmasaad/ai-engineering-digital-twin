"""Event-driven synchronization between the virtual twin and the repository.

The stored scan is the virtual state. A fresh scan is the real state. The
difference is a list of events. Applying those events replaces the virtual
structure with the repository, and the temporal graph records the reconciliation.
"""

from __future__ import annotations

MAX_EVENTS = 200

STRUCTURAL = ("language", "service", "role", "loc", "complexity", "todos")


def compare(previous: dict | None, observed: dict) -> dict:
    """Events that would make ``previous`` match ``observed``.

    ``previous is None`` is the first observation, one acceptance event rather
    than one event per module.
    """
    if previous is None:
        modules = [module for module in observed.get("modules") or [] if module.get("role") == "code"]
        event = _event(
            "observed",
            observed.get("name") or "repository",
            f"{len(modules)} modules",
            (
                f"Accepted the first observation: {len(modules)} code modules, "
                f"{len(observed.get('edges') or [])} imports, {len(observed.get('events') or [])} history events."
            ),
        )
        return _report([event], "accepted")

    events: list[dict] = []
    events.extend(_module_events(previous, observed))
    events.extend(_edge_events(previous, observed))
    events.extend(_history_events(previous, observed))
    events.extend(_dependency_events(previous, observed))
    events.extend(_signal_events(previous, observed))
    status = "aligned" if not events else "drifted"
    return _report(events, status)


def unmatched(virtual_events: list[dict], observed_events: list[dict]) -> list[dict]:
    """Facts the twin recorded that the repository history does not contain."""
    seen = {event_key(event) for event in observed_events}
    extra = []
    for event in virtual_events:
        if event_key(event) in seen:
            continue
        extra.append(
            _event(
                "virtual_only",
                event.get("type") or "event",
                event.get("service") or ", ".join(event.get("files") or []) or "twin",
                event.get("summary") or "Recorded on the twin.",
            )
        )
    return _cap(extra)


def event_key(event: dict) -> tuple:
    data = event.get("data") or {}
    files = tuple(sorted(str(path) for path in event.get("files") or []))
    if event.get("clock") == "ts":
        when = event.get("ts")
    else:
        when = round(float(event.get("age_days") or 0), 2)
    return (
        event.get("type"),
        when,
        event.get("summary"),
        event.get("service"),
        files,
        data.get("author"),
        data.get("severity"),
        data.get("result"),
        data.get("hours"),
    )


def _module_events(previous: dict, observed: dict) -> list[dict]:
    before = {module["path"]: module for module in previous.get("modules") or []}
    after = {module["path"]: module for module in observed.get("modules") or []}
    events = []
    for path in sorted(set(before) - set(after)):
        events.append(_event("module_removed", path, before[path].get("service") or "", f"{path} is no longer in the repository."))
    for path in sorted(set(after) - set(before)):
        module = after[path]
        events.append(
            _event(
                "module_added",
                path,
                module.get("service") or "",
                f"{path} appeared ({module.get('loc', 0)} lines, {module.get('role')}).",
            )
        )
    for path in sorted(set(before) & set(after)):
        changed = _module_delta(before[path], after[path])
        if changed:
            events.append(_event("module_changed", path, after[path].get("service") or "", f"{path} changed: {changed}."))
    return events


def _module_delta(before: dict, after: dict) -> str:
    parts = []
    for field in STRUCTURAL:
        old = before.get(field)
        new = after.get(field)
        if old != new:
            parts.append(f"{field} {old} → {new}")
    old_kinds = sorted({item.get("kind") for item in before.get("evidence") or []})
    new_kinds = sorted({item.get("kind") for item in after.get("evidence") or []})
    if old_kinds != new_kinds:
        parts.append(f"evidence {', '.join(old_kinds) or 'none'} → {', '.join(new_kinds) or 'none'}")
    return ", ".join(parts)


def _edge_events(previous: dict, observed: dict) -> list[dict]:
    before = {(edge["source"], edge["target"]) for edge in previous.get("edges") or []}
    after = {(edge["source"], edge["target"]) for edge in observed.get("edges") or []}
    events = []
    for source, target in sorted(before - after):
        events.append(_event("import_removed", source, target, f"{_short(source)} no longer imports {_short(target)}."))
    for source, target in sorted(after - before):
        events.append(_event("import_added", source, target, f"{_short(source)} imports {_short(target)}."))
    return events


def _history_events(previous: dict, observed: dict) -> list[dict]:
    before = {event_key(event): event for event in previous.get("events") or []}
    after = {event_key(event): event for event in observed.get("events") or []}
    events = []
    for key in after:
        if key in before:
            continue
        event = after[key]
        events.append(
            _event(
                "history_added",
                event.get("type") or "event",
                event.get("service") or ", ".join(event.get("files") or []) or "history",
                event.get("summary") or "New history event.",
            )
        )
    for key in before:
        if key in after:
            continue
        event = before[key]
        events.append(
            _event(
                "history_removed",
                event.get("type") or "event",
                event.get("service") or ", ".join(event.get("files") or []) or "history",
                f"History no longer contains: {event.get('summary') or 'event'}.",
            )
        )
    return events


def _dependency_events(previous: dict, observed: dict) -> list[dict]:
    before = set(previous.get("dependencies") or [])
    after = set(observed.get("dependencies") or [])
    events = []
    for name in sorted(after - before):
        events.append(_event("dependency_added", name, "manifest", f"Dependency {name} was declared."))
    for name in sorted(before - after):
        events.append(_event("dependency_removed", name, "manifest", f"Dependency {name} was removed."))
    return events


def _signal_events(previous: dict, observed: dict) -> list[dict]:
    before = previous.get("signals") or {}
    after = observed.get("signals") or {}
    if before.get("has_health") == after.get("has_health"):
        return []
    state = "present" if after.get("has_health") else "absent"
    return [_event("signal_changed", "has_health", state, f"A health check is {state}." )]


def _report(events: list[dict], status: str) -> dict:
    capped = _cap(events)
    return {
        "status": status,
        "applied": False,
        "summary": _summary(capped, status),
        "events": capped,
        "event_count": len(capped),
        "virtual_only": [],
    }


def _summary(events: list[dict], status: str) -> str:
    if status == "accepted" and events:
        return events[0]["summary"]
    if not events or status == "aligned":
        return "Virtual state matches the repository."
    counts: dict[str, int] = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    parts = [f"{count} {kind.replace('_', ' ')}" for kind, count in counts.items()]
    return "Repository moved: " + ", ".join(parts) + "."


def _cap(events: list[dict]) -> list[dict]:
    if len(events) <= MAX_EVENTS:
        return events
    extra = len(events) - MAX_EVENTS
    return events[:MAX_EVENTS] + [
        _event("overflow", "sync", str(extra), f"{extra} further changes were applied without their own event.")
    ]


def _event(kind: str, subject: str, obj: str, summary: str) -> dict:
    return {"kind": kind, "subject": subject, "object": obj, "summary": summary}


def _short(path: str) -> str:
    return path.rsplit("/", 1)[-1]
