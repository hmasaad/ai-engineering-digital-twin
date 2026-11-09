"""Blast radius of a proposed change against the current twin."""

from __future__ import annotations

from collections import Counter

from app.graph import adjacency, reachable


def assess_change(view: dict, modules: list[str], title: str = "Proposed change", note: str = "") -> dict:
    by_path = {module["path"]: module for module in view["modules"]}
    missing = [path for path in modules if path not in by_path or by_path[path]["role"] != "code"]
    if missing:
        raise KeyError(f"Unknown module: {', '.join(missing)}")
    if not modules:
        raise KeyError("Select at least one module")

    depths = reachable(modules, adjacency(view["edges"], reverse=True), max_depth=3)
    forward = adjacency(view["edges"], reverse=False)
    blast = []
    for path, depth in sorted(depths.items(), key=lambda item: (item[1], item[0])):
        module = by_path[path]
        blast.append(
            {
                "module": path,
                "service": module["service"],
                "depth": depth,
                "bug": module["risks"]["bug"],
                "delay": module["risks"]["delay"],
                "test_gap": module["test_gap"],
                "relation": "changed" if depth == 0 else "dependent",
            }
        )

    survival = 1.0
    for item in blast:
        exposure = 1.0 if item["depth"] == 0 else 0.55 / item["depth"]
        local = item["bug"] * (0.4 + 0.6 * item["test_gap"]) * exposure
        survival *= 1.0 - max(0.0, min(0.9, local))
    touched = {item["module"] for item in blast}
    objections = [
        {
            "agent": finding["agent"],
            "severity": finding["severity"],
            "title": finding["title"],
            "remediation": finding["remediation"],
        }
        for finding in view["findings"]
        if finding["severity"] in {"critical", "high"} and set(finding["modules"]) & touched
    ][:4]
    missing_tests = [item["module"] for item in blast if item["depth"] == 0 and item["test_gap"] >= 0.5]
    author_counts: Counter[str] = Counter()
    for item in blast:
        if item["depth"] > 1:
            continue
        for author in by_path[item["module"]]["authors"]:
            author_counts[author] += 1
    contracts = []
    for path in modules:
        for dependency in forward.get(path, []):
            contracts.append({"module": dependency, "via": path})

    direct = sum(1 for item in blast if item["depth"] == 0)
    indirect = len(blast) - direct
    sentences = []
    if missing_tests:
        sentences.append(f"Add or update tests for {', '.join(missing_tests)} before merging.")
    if objections:
        sentences.append(f"{objections[0]['agent']} objects: {objections[0]['title']}.")
    sentences.append(f"The change touches {direct} module{'s' if direct != 1 else ''} directly and {indirect} more through imports.")
    if note:
        sentences.append(note)

    return {
        "title": title,
        "note": note,
        "risk": round(1.0 - survival, 4),
        "delay_pressure": round(max(item["delay"] for item in blast if item["depth"] == 0), 4),
        "blast": blast,
        "missing_tests": missing_tests,
        "reviewers": [name for name, _count in author_counts.most_common(4)],
        "contracts": contracts,
        "objections": objections,
        "recommendation": " ".join(sentences),
    }
