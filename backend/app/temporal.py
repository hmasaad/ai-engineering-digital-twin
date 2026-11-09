"""Time-varying twin state and a temporal knowledge graph.

A dynamic twin is a function of time. ``project`` keeps only the events that
had already happened, shifts their ages so "now" means the chosen day, and
drops modules that had not been committed yet.

Relations are not permanent. Each fact is an interval ``[to_age, from_age]``
measured in days before the scan (``from_age`` is the older bound), or a point
event. An open interval has ``to_age is None`` and is still true at the scan.
"""

from __future__ import annotations

import copy

from app.score import code_edges, merge_models, observables_from_raw, score_observables

PREDICATES = {
    "exists": "Module exists",
    "imports": "Imports",
    "owns": "Ownership",
    "changed": "Changed",
    "reviewed": "Reviewed",
    "incident_on": "Incident",
    "deployed": "Deployed",
    "reconciled": "Reconciled",
    "depends_on": "Depends on",
    "plugin": "Native plugin",
    "modifies": "Pull request",
    "affects": "Affects",
    "covers": "Test covers",
    "failed": "Build failure",
    "attributed_to": "Attributed to",
    "built": "Build",
}


def build_dynamics(raw: dict, models: dict | None = None) -> dict:
    active = merge_models(models)
    births = birth_ages(raw)
    facts = knowledge_graph(raw, births)
    ages = _sample_ages(raw.get("events") or [])
    slices = [_slice(raw, age, active, births) for age in ages]
    slices.sort(key=lambda item: item["as_of"], reverse=True)
    for index, item in enumerate(slices):
        older = slices[index - 1] if index else None
        item["change"] = _change(older, item, raw.get("events") or [])
    return {
        "slices": slices,
        "facts": facts,
        "predicates": [{"key": key, "label": label} for key, label in PREDICATES.items()],
        "note": (
            "Each slice replays only the history that had already happened, then scores that past system "
            "with the current weights. A dependency upgrade closes the previous version. A pull request "
            "is valid from open until merge. A build failure is a point event, linked to a package when "
            "the history names one. Import and coverage facts start when both files exist, because the "
            "scanner sees structure once, not a separate graph for every commit."
        ),
    }


def project(raw: dict, as_of: float) -> dict:
    """Repository as it had been recorded ``as_of`` days before the scan."""
    moment = float(as_of)
    births = birth_ages(raw)

    def alive(module: dict) -> bool:
        if module["role"] != "code":
            return True
        born = births.get(module["path"])
        if born is None:
            return moment <= 1e-9
        return born + 1e-9 >= moment

    modules = [module for module in raw["modules"] if alive(module)]
    living = {module["path"] for module in modules}
    edges = [edge for edge in raw["edges"] if edge["source"] in living and edge["target"] in living]
    events = []
    for event in raw.get("events") or []:
        age = float(event["age_days"])
        if age + 1e-9 < moment:
            continue
        cloned = copy.deepcopy(event)
        cloned["age_days"] = round(age - moment, 4)
        events.append(cloned)
    return {**raw, "modules": modules, "edges": edges, "events": events}


def birth_ages(raw: dict) -> dict[str, float]:
    """Oldest commit age for each path. Missing means the file was only observed at the scan."""
    ages: dict[str, float] = {}
    for event in raw.get("events") or []:
        if event.get("type") != "commit":
            continue
        age = float(event["age_days"])
        stats = (event.get("data") or {}).get("file_stats") or {}
        paths = list(stats) or list(event.get("files") or [])
        for path in paths:
            ages[path] = max(ages.get(path, 0.0), age)
    return ages


def knowledge_graph(raw: dict, births: dict[str, float] | None = None) -> list[dict]:
    births = birth_ages(raw) if births is None else births
    lasts = _last_ages(raw)
    facts: list[dict] = []
    roles = {module["path"]: module for module in raw["modules"]}

    for path, module in sorted(roles.items()):
        if module["role"] != "code":
            continue
        born = births.get(path)
        if born is None:
            facts.append(
                _interval("exists", path, module["service"], 0.0, None, "Observed at the scan; no earlier commit.")
            )
        else:
            facts.append(_interval("exists", path, module["service"], born, None, "From the first recorded commit."))

    code_paths = {path for path, module in roles.items() if module["role"] == "code"}
    for edge in raw["edges"]:
        if edge["source"] not in code_paths or edge["target"] not in code_paths:
            continue
        start = _edge_start(births, edge["source"], edge["target"])
        facts.append(
            _interval(
                "imports",
                edge["source"],
                edge["target"],
                start,
                None,
                "Current import, valid once both files exist.",
            )
        )

    ownership: dict[tuple[str, str], list[float]] = {}
    for event in raw.get("events") or []:
        age = float(event["age_days"])
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "commit":
            author = data.get("author") or "unknown"
            stats = data.get("file_stats") or {}
            paths = list(stats) or list(event.get("files") or [])
            for path in paths:
                facts.append(_event("changed", author, path, age, event.get("summary") or "commit"))
                ownership.setdefault((author, path), []).append(age)
        elif kind == "review":
            for path in event.get("files") or []:
                hours = data.get("hours")
                label = event.get("summary") or (f"{hours}h review" if hours else "review")
                facts.append(_event("reviewed", path, "review", age, label))
        elif kind == "incident" and event.get("service"):
            severity = data.get("severity") or "incident"
            facts.append(_event("incident_on", severity, event["service"], age, event.get("summary") or severity))
        elif kind == "deploy" and event.get("service"):
            result = data.get("result") or "deploy"
            facts.append(_event("deployed", event["service"], result, age, event.get("summary") or result))

    for (author, path), touches in sorted(ownership.items()):
        start = max(touches)
        end = min(touches)
        latest = lasts.get(path)
        open_ended = latest is not None and abs(end - latest) < 1e-6
        facts.append(
            _interval(
                "owns",
                author,
                path,
                start,
                None if open_ended else end,
                "Latest author." if open_ended else "Committed during this span.",
            )
        )
    facts.extend(_engineering_facts(raw, births, roles))
    facts.sort(key=lambda fact: (-(fact.get("from_age") if fact["kind"] == "interval" else fact["at_age"]), fact["predicate"]))
    return facts


def holds(fact: dict, as_of: float, window: float = 20.0) -> bool:
    moment = float(as_of)
    if fact["kind"] == "event":
        age = float(fact["at_age"])
        return moment <= age < moment + window
    if moment > float(fact["from_age"]) + 1e-6:
        return False
    end = fact.get("to_age")
    if end is not None and moment < float(end) - 1e-6:
        return False
    return True


def _slice(raw: dict, as_of: float, models: dict, births: dict[str, float]) -> dict:
    projected = project(raw, as_of)
    views = score_observables(observables_from_raw(projected), projected["edges"], models)
    code = [module for module in views if module["role"] == "code"]
    imports = code_edges(views, projected["edges"])
    authors = sorted({author for module in code for author in module["authors"]})
    top = max(code, key=lambda module: module["risks"]["bug"]) if code else None
    incident = max((module["risks"]["incident"] for module in code), default=0.0)
    bug = max((module["risks"]["bug"] for module in code), default=0.0)
    paths = sorted(module["path"] for module in code)
    return {
        "as_of": as_of,
        "label": "now" if as_of == 0 else f"{int(as_of)}d ago",
        "modules": len(code),
        "module_paths": paths,
        "edges": len(imports),
        "bug": round(bug, 4),
        "incident": round(incident, 4),
        "state_index": _state_index(code),
        "top_module": None if top is None else top["path"],
        "authors": authors,
        "graph": {
            "nodes": [
                {
                    "path": module["path"],
                    "service": module["service"],
                    "bug": module["risks"]["bug"],
                    "born": births.get(module["path"]),
                }
                for module in sorted(code, key=lambda module: module["path"])
            ],
            "imports": [{"source": edge["source"], "target": edge["target"]} for edge in imports],
        },
    }


def _change(older: dict | None, current: dict, events: list[dict]) -> str:
    if older is None:
        return "Start of the sampled window."
    appeared = [path for path in current["module_paths"] if path not in set(older["module_paths"])]
    between = [
        event
        for event in events
        if current["as_of"] <= float(event["age_days"]) < older["as_of"]
    ]
    parts = []
    if appeared:
        shown = ", ".join(_short(path) for path in appeared[:3])
        extra = f" (+{len(appeared) - 3})" if len(appeared) > 3 else ""
        noun = "module" if len(appeared) == 1 else "modules"
        verb = "appears" if len(appeared) == 1 else "appear"
        parts.append(f"{len(appeared)} {noun} {verb}: {shown}{extra}.")
    incidents = [event for event in between if event.get("type") == "incident"]
    if incidents:
        parts.append(incidents[0].get("summary") or "An incident is recorded.")
    commits = sum(1 for event in between if event.get("type") == "commit")
    if commits:
        parts.append(f"{commits} commit{'s' if commits != 1 else ''} land.")
    upgrades = [event for event in between if event.get("type") == "dependency"]
    if upgrades:
        parts.append(upgrades[0].get("summary") or "A dependency is upgraded.")
    failures = [
        event
        for event in between
        if event.get("type") == "build" and (event.get("data") or {}).get("result") == "failure"
    ]
    if failures:
        parts.append(failures[0].get("summary") or "A build fails.")
    return " ".join(parts) or "No recorded change in this interval."


def _sample_ages(events: list[dict]) -> list[float]:
    if not events:
        return [0.0]
    oldest = max(float(event["age_days"]) for event in events)
    ages = []
    cursor = 0.0
    while cursor < oldest and len(ages) < 8:
        ages.append(cursor)
        cursor += 20.0
    if not ages:
        ages = [0.0]
    return ages


def _engineering_facts(raw: dict, births: dict[str, float], roles: dict[str, dict]) -> list[dict]:
    """Package, plugin, pull request, test, and build relations with validity intervals."""
    facts: list[dict] = []
    upgrades: dict[tuple[str, str], list[dict]] = {}
    for event in raw.get("events") or []:
        data = event.get("data") or {}
        if event.get("type") == "dependency" and data.get("package"):
            key = (str(data.get("manifest") or ""), str(data["package"]))
            upgrades.setdefault(key, []).append(event)
    for history in upgrades.values():
        history.sort(key=lambda event: float(event["age_days"]), reverse=True)

    seen: set[tuple[str, str]] = set()
    for spec in raw.get("packages") or []:
        name = str(spec.get("name") or "")
        if not name:
            continue
        manifest = str(spec.get("manifest") or "manifest")
        version = str(spec.get("version") or "")
        key = (manifest, name)
        seen.add(key)
        history = upgrades.get(key) or []
        if history:
            latest = history[-1]
            data = latest.get("data") or {}
            pinned = str(data.get("version") or version)
            label = f"{name}@{pinned}" if pinned else name
            start = float(latest["age_days"])
            facts.append(
                _interval(
                    "depends_on",
                    manifest,
                    label,
                    start,
                    None,
                    latest.get("summary") or f"{name} from the recorded upgrade.",
                )
            )
            prior = data.get("prior")
            if prior:
                older = float(history[-2]["age_days"]) if len(history) > 1 else _older_bound(raw, start)
                facts.append(
                    _interval(
                        "depends_on",
                        manifest,
                        f"{name}@{prior}",
                        older,
                        start,
                        f"{name} {prior} until the upgrade.",
                    )
                )
        else:
            label = f"{name}@{version}" if version else name
            facts.append(
                _interval(
                    "depends_on",
                    manifest,
                    label,
                    0.0,
                    None,
                    "Declared in the manifest. No earlier pin was logged.",
                )
            )
        for plugin in spec.get("plugins") or []:
            facts.append(
                _interval("plugin", name, str(plugin), 0.0, None, "Native binding declared in the manifest.")
            )

    for (manifest, name), history in sorted(upgrades.items()):
        if (manifest, name) in seen:
            continue
        latest = history[-1]
        data = latest.get("data") or {}
        pinned = str(data.get("version") or "")
        facts.append(
            _interval(
                "depends_on",
                manifest or name,
                f"{name}@{pinned}" if pinned else name,
                float(latest["age_days"]),
                0.0,
                "Recorded in history and absent from the current manifest.",
            )
        )

    for event in raw.get("events") or []:
        age = float(event["age_days"])
        kind = event.get("type")
        data = event.get("data") or {}
        summary = event.get("summary") or ""
        if kind == "pull_request":
            subject = f"pr:{data.get('id') or 'pr'}"
            end = _closed(data, age)
            for path in event.get("files") or []:
                facts.append(_interval("modifies", subject, path, age, end, summary or "Pull request."))
            for test in data.get("tests") or []:
                facts.append(_interval("affects", subject, test, age, end, summary or "Affected tests."))
        elif kind == "build":
            subject = f"build:{data.get('id') or 'build'}"
            result = data.get("result") or "result"
            if result == "failure":
                facts.append(_event("failed", subject, result, age, summary or "Build failure."))
                if data.get("package"):
                    facts.append(
                        _event(
                            "attributed_to",
                            subject,
                            str(data["package"]),
                            age,
                            summary or f"Failure associated with {data['package']}.",
                        )
                    )
            else:
                facts.append(_event("built", subject, str(result), age, summary or "Build."))
            for test in data.get("tests") or []:
                facts.append(_event("affects", subject, test, age, summary or "Build ran this test."))
        elif kind == "plugin" and data.get("package") and data.get("plugin"):
            until = data.get("merged_age")
            facts.append(
                _interval(
                    "plugin",
                    str(data["package"]),
                    str(data["plugin"]),
                    age,
                    None if until is None else float(until),
                    summary or "Package binds a native plugin.",
                )
            )

    for edge in raw.get("edges") or []:
        source = roles.get(edge["source"])
        target = roles.get(edge["target"])
        if not source or not target or source.get("role") != "test" or target.get("role") != "code":
            continue
        facts.append(
            _interval(
                "covers",
                edge["source"],
                edge["target"],
                _edge_start(births, edge["source"], edge["target"]),
                None,
                "Test imports this module. Valid once both files exist.",
            )
        )
    return facts


def _closed(data: dict, opened: float) -> float | None:
    merged = data.get("merged_age")
    if merged is not None:
        return float(merged)
    if data.get("status") == "merged":
        return opened
    return None


def _older_bound(raw: dict, upgrade_age: float) -> float:
    older = [float(event["age_days"]) for event in raw.get("events") or [] if float(event["age_days"]) > upgrade_age + 1e-6]
    return max(older) if older else upgrade_age


def _edge_start(births: dict[str, float], source: str, target: str) -> float:
    ends = [births[path] for path in (source, target) if path in births]
    if len(ends) < 2:
        return 0.0
    return min(ends)


def _last_ages(raw: dict) -> dict[str, float]:
    ages: dict[str, float] = {}
    for event in raw.get("events") or []:
        if event.get("type") != "commit":
            continue
        age = float(event["age_days"])
        stats = (event.get("data") or {}).get("file_stats") or {}
        paths = list(stats) or list(event.get("files") or [])
        for path in paths:
            ages[path] = min(ages.get(path, age), age)
    return ages


def _interval(predicate: str, subject: str, obj: str, from_age: float, to_age: float | None, summary: str) -> dict:
    return {
        "kind": "interval",
        "predicate": predicate,
        "subject": subject,
        "object": obj,
        "from_age": round(float(from_age), 2),
        "to_age": None if to_age is None else round(float(to_age), 2),
        "summary": summary,
    }


def reconciled_facts(events: list[dict], at_age: float) -> list[dict]:
    """Point facts for a sync that brought the virtual twin back onto the repository."""
    age = round(float(at_age), 2)
    return [
        _event("reconciled", event["kind"], event.get("object") or event.get("subject") or "", age, event["summary"])
        for event in events
        if event.get("kind") != "overflow"
    ]


def _event(predicate: str, subject: str, obj: str, at_age: float, summary: str) -> dict:
    return {
        "kind": "event",
        "predicate": predicate,
        "subject": subject,
        "object": obj,
        "at_age": round(float(at_age), 2),
        "summary": summary,
    }


def _state_index(code: list[dict]) -> int:
    if not code:
        return 100
    bugs = sorted((module["risks"]["bug"] for module in code), reverse=True)
    top = sum(bugs[:3]) / min(3, len(bugs))
    incident = max(module["risks"]["incident"] for module in code)
    return int(round(max(0.0, min(100.0, 100 - 45 * top - 30 * incident))))


def _short(path: str) -> str:
    return path.rsplit("/", 1)[-1]
