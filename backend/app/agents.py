"""Specialized reviewers and the coordinator that sequences their findings."""

from __future__ import annotations

from collections import defaultdict

from app.graph import find_cycles

ROSTER = [
    ("Security", "Secrets, injection, and transport."),
    ("Reliability", "Incidents, rollbacks, and operability."),
    ("Performance", "Hot paths, unbounded work, and query shape."),
    ("Quality", "Tests, complexity, and unfinished work."),
    ("Architect", "Boundaries, cycles, and coupling."),
    ("Delivery", "Review lag, ownership, and change collision."),
]

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
AGENT_RANK = {name: index for index, (name, _mandate) in enumerate(ROSTER)}


def analyze(modules: list[dict], edges: list[dict], context: dict) -> list[dict]:
    findings: list[dict] = []
    findings.extend(_cap(_security(modules), 4))
    findings.extend(_cap(_reliability(modules, context), 4))
    findings.extend(_cap(_performance(modules), 3))
    findings.extend(_cap(_quality(modules, context), 3))
    findings.extend(_cap(_architect(modules, edges), 4))
    findings.extend(_cap(_delivery(modules), 3))
    for index, finding in enumerate(findings, start=1):
        finding["id"] = f"f{index}"
    return findings


def coordinate(findings: list[dict]) -> list[dict]:
    ranked = sorted(
        findings,
        key=lambda finding: (
            SEVERITY_RANK.get(finding["severity"], 9),
            AGENT_RANK.get(finding["agent"], 9),
            finding["title"],
        ),
    )
    steps = []
    for finding in ranked[:8]:
        steps.append(
            {
                "step": len(steps) + 1,
                "agent": finding["agent"],
                "severity": finding["severity"],
                "title": finding["title"],
                "detail": finding["detail"],
                "modules": finding["modules"],
                "effort": finding["effort"],
                "remediation": finding["remediation"],
                "expected_effect": finding["expected_effect"],
            }
        )
    for index, step in enumerate(steps):
        later = [
            other
            for other in steps[index + 1 :]
            if set(other["modules"]) & set(step["modules"])
        ]
        if later and later[0]["modules"]:
            step["handoff"] = f"Unblocks step {later[0]['step']} on {later[0]['modules'][0]}."
        else:
            step["handoff"] = "Can proceed without waiting on a later step."
    return steps


def _security(modules: list[dict]) -> list[dict]:
    severity = {
        "hardcoded-secret": "critical",
        "sql-injection": "critical",
        "dangerous-exec": "critical",
        "unverified-tls": "high",
    }
    titles = {
        "hardcoded-secret": "Hardcoded credential",
        "sql-injection": "SQL built with an f-string",
        "dangerous-exec": "Dynamic code execution",
        "unverified-tls": "TLS verification disabled",
    }
    remediation = {
        "hardcoded-secret": "Read the token from the environment and fail closed when it is missing.",
        "sql-injection": "Use a parameterized query and keep the value out of the SQL string.",
        "dangerous-exec": "Remove eval/exec from this path and call a specific function instead.",
        "unverified-tls": "Verify TLS. Trust the internal CA if this host uses one; do not disable verification.",
    }
    effect = {
        "hardcoded-secret": "Removes a credential that every importer of this module can see.",
        "sql-injection": "Closes the injection sink on the shared database helper.",
        "dangerous-exec": "Removes a code-execution sink.",
        "unverified-tls": "Stops this client from accepting a forged mail endpoint.",
    }
    findings = []
    for module in modules:
        for evidence in module["evidence"]:
            kind = evidence["kind"]
            if kind not in severity:
                continue
            findings.append(
                _finding(
                    agent="Security",
                    severity=severity[kind],
                    title=f"{titles[kind]} in {module['path']}",
                    detail=f"Line {evidence['line']}: {evidence['text']}",
                    modules=[module["path"]],
                    evidence=[evidence],
                    effort="S",
                    remediation=remediation[kind],
                    expected_effect=effect[kind],
                )
            )
    return findings


def _reliability(modules: list[dict], context: dict) -> list[dict]:
    findings = []
    seen_services: set[str] = set()
    for module in modules:
        service = module["service"]
        if module["role"] != "code" or service in seen_services or not module.get("incident_level"):
            continue
        seen_services.add(service)
        level = module["incident_level"]
        summary = module.get("incident_summary") or "recent incident"
        findings.append(
            _finding(
                agent="Reliability",
                severity="critical" if level == "sev1" else "high" if level == "sev2" else "medium",
                title=f"{service} still carries a {level}",
                detail=summary,
                modules=[item["path"] for item in modules if item["service"] == service and item["role"] == "code"][:6],
                evidence=[
                    {
                        "path": module["path"],
                        "line": 0,
                        "kind": "incident",
                        "text": f"{level}: {summary}",
                    }
                ],
                effort="M",
                remediation=f"Add a regression check that would have caught this before the next {service} release: {summary}.",
                expected_effect="Ties the next change to the failure the service already paid for.",
            )
        )
    for module in modules:
        for evidence in module["evidence"]:
            if evidence["kind"] != "container-root":
                continue
            findings.append(
                _finding(
                    agent="Reliability",
                    severity="high",
                    title=f"{module['path']} runs as root",
                    detail=evidence["text"],
                    modules=[module["path"]],
                    evidence=[evidence],
                    effort="S",
                    remediation="Set a non-root USER before the process starts.",
                    expected_effect="Shrinks the blast radius of a container compromise.",
                )
            )
    if not context.get("signals", {}).get("has_health"):
        gateway = next((module["path"] for module in modules if module["service"] == "gateway"), "gateway")
        findings.append(
            _finding(
                agent="Reliability",
                severity="medium",
                title="No health check in the runtime path",
                detail="No module exposes /health and the container has no HEALTHCHECK.",
                modules=[gateway] if gateway != "gateway" else [],
                evidence=[],
                effort="S",
                remediation="Add a dependency-aware health endpoint and a container HEALTHCHECK that calls it.",
                expected_effect="Gives deploys a signal other than customer traffic.",
            )
        )
    return findings


def _performance(modules: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    owners = {module["path"]: module for module in modules}
    for module in modules:
        for evidence in module["evidence"]:
            if evidence["kind"] in {"unbounded-loop", "blocking-sleep", "select-star"}:
                grouped[module["path"]].append(evidence)
    findings = []
    for path, evidence in grouped.items():
        kinds = {item["kind"] for item in evidence}
        if "unbounded-loop" in kinds:
            title = f"Unbounded retry in {path}"
            remediation = "Cap attempts, stop capturing again after success, and back off with a ceiling."
            effect = "Cuts duplicate work and the latency tail on this path."
            severity = "high"
            effort = "M"
        elif "select-star" in kinds:
            title = f"Unbounded read in {path}"
            remediation = "Select the columns this caller uses and bound the row set."
            effect = "Keeps the query from growing with the table."
            severity = "medium"
            effort = "S"
        else:
            title = f"Blocking wait in {path}"
            remediation = "Move the sleep off the caller or cap it."
            effect = "Removes a stall from this path."
            severity = "medium"
            effort = "S"
        findings.append(
            _finding(
                agent="Performance",
                severity=severity,
                title=title,
                detail="; ".join(item["text"] for item in evidence[:3]),
                modules=[path],
                evidence=evidence[:3],
                effort=effort,
                remediation=remediation,
                expected_effect=effect,
            )
        )
        owners.setdefault(path, None)
    return findings


def _quality(modules: list[dict], context: dict) -> list[dict]:
    findings = []
    code = [module for module in modules if module["role"] == "code"]
    hot = [
        module
        for module in code
        if module["test_gap"] >= 0.8 and module["features"].get("test_pressure", 0) >= 0.45
    ]
    hot.sort(key=lambda module: module["risks"]["bug"], reverse=True)
    if hot:
        lead = hot[0]
        findings.append(
            _finding(
                agent="Quality",
                severity="critical" if lead["risks"]["bug"] >= 0.75 else "high",
                title=f"Active code without tests: {lead['path']}",
                detail=(
                    f"Bug index {lead['risks']['bug']:.2f}. "
                    f"Decayed churn is {lead['churn']:.0f} lines and no test module imports this file."
                ),
                modules=[lead["path"]],
                evidence=[],
                effort="M",
                remediation="Add characterization tests around the current behavior before the next change.",
                expected_effect="Drops test pressure on the module the model already treats as active.",
            )
        )
    if len(hot) > 1:
        rest = [module["path"] for module in hot[1:4]]
        findings.append(
            _finding(
                agent="Quality",
                severity="medium",
                title="More untested hotspots",
                detail=", ".join(rest),
                modules=rest,
                evidence=[],
                effort="M",
                remediation="Cover these after the lead hotspot. They move, and nothing imports them from tests.",
                expected_effect="Removes the next cluster of thin-test pressure.",
            )
        )
    if context.get("undeclared"):
        names = ", ".join(context["undeclared"][:6])
        findings.append(
            _finding(
                agent="Quality",
                severity="medium",
                title="Imports missing from the manifest",
                detail=names,
                modules=[],
                evidence=[],
                effort="S",
                remediation=f"Declare {names} or remove the import.",
                expected_effect="Makes the runtime dependency set match the code.",
            )
        )
    return findings


def _architect(modules: list[dict], edges: list[dict]) -> list[dict]:
    findings = []
    by_path = {module["path"]: module for module in modules}
    cycles = find_cycles(edges)
    for cycle in cycles[:2]:
        names = [_short(path) for path in cycle]
        findings.append(
            _finding(
                agent="Architect",
                severity="critical",
                title=f"Import cycle: {' ↔ '.join(names)}",
                detail="These modules import each other, so a change cannot be reasoned about in one direction.",
                modules=cycle,
                evidence=[],
                effort="L",
                remediation=(
                    "Move the shared rule into a library neither service owns. "
                    "inventory/stock.py should not import checkout to validate a cart."
                ),
                expected_effect="Breaks the cycle and lowers coupling on both sides.",
            )
        )
    for edge in edges:
        source = by_path.get(edge["source"])
        target = by_path.get(edge["target"])
        if not source or not target:
            continue
        if source["layer"] == "libs" and target["layer"] == "services":
            findings.append(
                _finding(
                    agent="Architect",
                    severity="high",
                    title=f"Library {source['path']} imports service code",
                    detail=f"Target: {target['path']}",
                    modules=[source["path"], target["path"]],
                    evidence=[],
                    effort="M",
                    remediation="Invert the dependency. Libraries should not reach into services.",
                    expected_effect="Keeps the shared library deployable without the service.",
                )
            )
    peers = []
    for edge in edges:
        source = by_path.get(edge["source"])
        target = by_path.get(edge["target"])
        if not source or not target:
            continue
        if (
            source["layer"] == "services"
            and target["layer"] == "services"
            and source["service"] != target["service"]
            and source["service"] != "gateway"
        ):
            peers.append(f"{_short(source['path'])} → {_short(target['path'])}")
    if peers:
        findings.append(
            _finding(
                agent="Architect",
                severity="medium",
                title="Services reach into peer services",
                detail=", ".join(peers[:6]),
                modules=[],
                evidence=[],
                effort="L",
                remediation="Keep peer calls on an edge owned by the gateway or a shared contract, not on internal modules.",
                expected_effect="Makes the blast radius of a service change match its public edge.",
            )
        )
    fan_in: dict[str, int] = defaultdict(int)
    for edge in edges:
        fan_in[edge["target"]] += 1
    for module in modules:
        if module["role"] != "code":
            continue
        incoming = fan_in[module["path"]]
        if module["loc"] >= 200 and incoming >= 3:
            findings.append(
                _finding(
                    agent="Architect",
                    severity="high",
                    title=f"God module {module['path']}",
                    detail=f"{module['loc']} lines, complexity {module['complexity']}, fan-in {incoming}.",
                    modules=[module["path"]],
                    evidence=[],
                    effort="L",
                    remediation="Split the module along its callers. Leave a facade only if they share a contract.",
                    expected_effect="Lowers complexity and the number of reasons the file changes.",
                )
            )
        elif module["complexity"] >= 18:
            findings.append(
                _finding(
                    agent="Architect",
                    severity="high",
                    title=f"High branch complexity in {module['path']}",
                    detail=f"Complexity {module['complexity']} across {module['loc']} lines.",
                    modules=[module["path"]],
                    evidence=[],
                    effort="M",
                    remediation="Collapse branches that repeat the same decision, and name the retry budget in one place.",
                    expected_effect="Makes the next change local instead of a walk through every branch.",
                )
            )
    return findings


def _delivery(modules: list[dict]) -> list[dict]:
    findings = []
    for module in modules:
        if module["role"] != "code":
            continue
        if module["review_hours"] >= 24:
            findings.append(
                _finding(
                    agent="Delivery",
                    severity="high" if module["review_hours"] >= 48 else "medium",
                    title=f"Review lag on {module['path']} is {module['review_hours']:.0f}h",
                    detail="Slow review stacks with churn: the next change starts from a stale reading of the file.",
                    modules=[module["path"]],
                    evidence=[],
                    effort="S",
                    remediation="Set a review owner for this file and split changes so a review finishes inside two days.",
                    expected_effect="Cuts the instability term that the delay model reads from review hours.",
                )
            )
        if len(module["authors"]) == 1 and module["churn"] >= 80:
            findings.append(
                _finding(
                    agent="Delivery",
                    severity="high",
                    title=f"Single owner on active {module['path']}",
                    detail=f"Only {module['authors'][0]} has changed this file while it accumulated {module['churn']:.0f} decayed lines.",
                    modules=[module["path"]],
                    evidence=[],
                    effort="M",
                    remediation="Pair a second owner through the next change so the bus factor is not one person.",
                    expected_effect="Lowers ownership risk on a file that is still moving.",
                )
            )
    return findings


def _finding(
    agent: str,
    severity: str,
    title: str,
    detail: str,
    modules: list[str],
    evidence: list[dict],
    effort: str,
    remediation: str,
    expected_effect: str,
) -> dict:
    return {
        "agent": agent,
        "severity": severity,
        "title": title,
        "detail": detail,
        "modules": modules,
        "evidence": evidence,
        "effort": effort,
        "remediation": remediation,
        "expected_effect": expected_effect,
    }


def _cap(findings: list[dict], limit: int) -> list[dict]:
    findings.sort(key=lambda finding: (SEVERITY_RANK.get(finding["severity"], 9), finding["title"]))
    return findings[:limit]


def _short(path: str) -> str:
    return path.rsplit("/", 1)[-1]
