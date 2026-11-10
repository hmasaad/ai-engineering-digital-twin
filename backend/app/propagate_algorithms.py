"""Ten risk-propagation algorithms, each reading the step before it.

Risk moves from the imported file to the importer. A hop multiplies the parent
index by a transmission. A belief update combines overlapping messages with a
noisy-or. Causes that share a package or an import cycle are one common cause,
and a weaker signal of that same cause is a condition, not another index.
The result is a decision-support index. It does not rewrite the package index
or the bug, incident, delay, or performance indexes, and it is not a probability.
"""

from __future__ import annotations

from collections import defaultdict

from app.graph import find_cycles
from app.propagate import FAILURE, MIN_RISK, PLUGIN, UPGRADE, _combine, _pkg_name
from app.temporal import holds

HOP_SHARE = 0.55
TRANSMISSION = {
    "vulnerability": 0.85,
    "failure": 0.70,
    "dependency_change": 0.40,
    "architecture": 0.30,
}
PAST_DAYS = 20.0
TITLES = (
    "Graph-Based Risk Propagation Algorithms",
    "Weighted Dependency Risk Modeling",
    "Probabilistic Failure Propagation",
    "Bayesian Network-Based Risk Inference",
    "Cascading Failure and Fault Tree Analysis",
    "Correlated Risk and Common-Cause Failure Modeling",
    "Dynamic Risk Recalculation",
    "Risk Containment and Mitigation Optimization",
    "Uncertainty Quantification",
    "Risk Propagation Evaluation and Calibration",
)


def run_algorithms(view: dict, report: dict) -> dict:
    traces = list(report.get("traces") or [])
    by_path = {
        module["path"]: module
        for module in view.get("modules") or []
        if module.get("role") == "code"
    }
    cycles = find_cycles(view.get("edges") or [])
    graph = _graph(traces)
    weighted = _weight(graph, traces)
    probabilistic = _probabilistic(weighted, traces, by_path)
    bayesian = _bayesian(graph, traces, cycles)
    tree = _fault_tree(traces, bayesian)
    correlated = _correlate(traces, cycles, view)
    dynamic = _dynamic(view, correlated)
    containment = _contain(correlated, by_path, dynamic)
    uncertainty = _uncertainty(view, correlated, probabilistic)
    calibration = _calibrate(view, report, correlated)
    steps = [graph, weighted, probabilistic, bayesian, tree, correlated, dynamic, containment, uncertainty, calibration]
    return {
        "steps": steps,
        "note": (
            "The steps run in order. Risk moves from the imported file to the importer. "
            "Each hop multiplies the parent index. Overlapping messages meet in a noisy-or, so they are not added. "
            "A fault tree uses that product as an AND gate. Causes that share a package or a cycle count once, and a weaker signal of the same package is a condition. "
            "The index is replayed 20 days ago, then one containment cut is chosen. "
            "The band and the calibration check sit on that common-cause index. "
            "None of these steps rewrites the package index or the bug, incident, delay, or performance indexes. "
            "A conditional index is not a probability."
        ),
    }


def _step(order: int, title: str, summary: str, rows: list[dict], **extra) -> dict:
    return {"order": order, "title": title, "summary": summary, "rows": rows, **extra}


def _hop(kind: str) -> float:
    return round(HOP_SHARE * TRANSMISSION[kind], 4)


def _evidence(module: dict | None) -> float:
    gap = 0.0 if module is None else float(module.get("test_gap") or 0)
    return round(0.45 + 0.55 * gap, 4)


def _graph(traces: list[dict]) -> dict:
    edges = []
    for trace in traces:
        for row in trace.get("affected") or []:
            chain = row.get("path") or []
            if len(chain) < 2:
                continue
            edges.append(
                {
                    "source": chain[-2],
                    "target": chain[-1],
                    "depth": row["depth"],
                    "origin": trace["origin"],
                    "kind": trace["kind"],
                }
            )
    edges.sort(key=lambda item: (item["depth"], item["source"], item["target"], item["origin"]))
    rows = [
        {
            "title": f"{edge['source']} → {edge['target']}",
            "detail": (
                f"depth {edge['depth']} · {edge['kind']} · dependency to importer · from {edge['origin']}"
            ),
        }
        for edge in edges[:8]
    ]
    if edges:
        summary = (
            f"Risk moves along {len(edges)} directed edges, from the imported file to the importer. "
            "Node indexes are not added."
        )
    else:
        summary = "No import carries a weakness onward. Node indexes are not added."
    return _step(1, TITLES[0], summary, rows, edges=edges)


def _weight(graph: dict, traces: list[dict]) -> dict:
    scores = {trace["origin"]: float(trace["severity_score"]) for trace in traces}
    weighted = []
    for edge in graph["edges"]:
        weight = _hop(edge["kind"])
        carried = round(scores.get(edge["origin"], 0.0) * (weight ** edge["depth"]), 4)
        weighted.append({**edge, "weight": weight, "carried": carried, "origin_index": scores.get(edge["origin"], 0.0)})
    rows = [
        {
            "title": f"{edge['source']} → {edge['target']}",
            "detail": (
                f"weight {edge['weight']:.4f} · carried {edge['carried']:.4f} · "
                f"multiplies the origin index {edge['origin_index']:.4f}"
            ),
        }
        for edge in weighted[:8]
    ]
    if weighted:
        summary = (
            f"Each hop keeps a transmission of {HOP_SHARE:.2f} times the cause factor. "
            "The carried index multiplies the origin. It does not add the importer's own index."
        )
    else:
        summary = "There is no hop to weight. The origin index would be multiplied, not added to another node."
    return _step(2, TITLES[1], summary, rows, edges=weighted)


def _probabilistic(weighted: dict, traces: list[dict], by_path: dict) -> dict:
    rows_by_key = {}
    for trace in traces:
        hop = _hop(trace["kind"])
        origin = float(trace["severity_score"])
        for row in trace.get("affected") or []:
            module = by_path.get(row["module"])
            evidence = _evidence(module)
            index = round(origin * (hop ** row["depth"]) * evidence, 4)
            key = (trace["origin"], row["module"])
            rows_by_key[key] = {
                "origin": trace["origin"],
                "module": row["module"],
                "depth": row["depth"],
                "evidence": evidence,
                "index": index,
            }
    ordered = sorted(rows_by_key.values(), key=lambda item: (item["depth"] == 0, item["evidence"], item["depth"], item["module"]))
    rows = []
    for item in ordered[:8]:
        if item["depth"] == 0:
            how = "origin, before any hop"
        else:
            noun = "condition" if item["depth"] == 1 else "conditions"
            how = f"product of {item['depth']} hop {noun}"
        rows.append(
            {
                "title": item["module"],
                "detail": (
                    f"conditional index {item['index']:.4f} · {how} · "
                    f"evidence {item['evidence']:.2f} · from {item['origin']}"
                ),
            }
        )
    if ordered:
        summary = (
            "The conditional index is the product of the hop transmission and the local test condition. "
            "It is not a probability, and it is not the sum of the files on the path."
        )
    else:
        summary = "There is no path to condition. A conditional index would be a product, not a probability and not a sum."
    return _step(3, TITLES[2], summary, rows, rows_by_module=ordered)


def _bayesian(graph: dict, traces: list[dict], cycles: list[list[str]]) -> dict:
    arc: dict[tuple[str, str], dict] = {}
    for edge in graph["edges"]:
        key = (edge["source"], edge["target"])
        hop = _hop(edge["kind"])
        current = arc.get(key)
        if current is None or hop > current["hop"]:
            arc[key] = {**edge, "hop": hop}
    dropped = _drop_cycle_edges(arc, cycles)
    for key in dropped:
        arc.pop(key, None)
    priors = {trace["origin"]: float(trace["severity_score"]) for trace in traces}
    nodes = set(priors) | {node for pair in arc for node in pair}
    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[str]] = defaultdict(list)
    indeg = {node: 0 for node in nodes}
    for source, target in arc:
        outgoing[source].append(target)
        incoming[target].append(source)
        indeg[target] = indeg.get(target, 0) + 1
    queue = sorted(node for node, degree in indeg.items() if degree == 0)
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in sorted(outgoing.get(node, [])):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
                queue.sort()
    beliefs: dict[str, float] = {}
    sums: dict[str, float] = {}
    messages: dict[str, list[float]] = {}
    for node in order:
        parts = []
        local = priors.get(node, 0.0)
        if local:
            parts.append(local)
        delivered = []
        for parent in sorted(incoming.get(node, [])):
            if parent not in beliefs:
                continue
            delivered.append(round(beliefs[parent] * arc[(parent, node)]["hop"], 4))
        parts.extend(delivered)
        beliefs[node] = _combine(parts) if parts else 0.0
        sums[node] = round(sum(parts), 4)
        messages[node] = delivered
    rows = []
    ranked = sorted(beliefs, key=lambda item: (-(sums[item] - beliefs[item]), -beliefs[item], item))
    for node in ranked[:5]:
        count = len(messages.get(node) or [])
        noun = "message" if count == 1 else "messages"
        rows.append(
            {
                "title": node,
                "detail": (
                    f"belief {beliefs[node]:.4f} · noisy-or of the prior and {count} {noun} · "
                    f"adding those parts would be {sums[node]:.4f}"
                ),
            }
        )
    for source, target in sorted(dropped)[:2]:
        row = {
            "title": f"{source} → {target}",
            "detail": "cycle · this direction does not send a second message",
        }
        if rows:
            rows.insert(1, row)
        else:
            rows.append(row)
    if beliefs:
        summary = (
            "Each node has the files it imports as parents. "
            "The belief is a noisy-or of the local prior and the messages those parents send. "
            "The messages are not added."
        )
    else:
        summary = "There is no node to update. A belief would be a noisy-or of parent messages, not a sum."
    return _step(
        4,
        TITLES[3],
        summary,
        rows,
        beliefs=beliefs,
        sums=sums,
        messages=messages,
        dropped=[{"source": source, "target": target} for source, target in sorted(dropped)],
    )


def _drop_cycle_edges(arc: dict[tuple[str, str], dict], cycles: list[list[str]]) -> set[tuple[str, str]]:
    dropped: set[tuple[str, str]] = set()
    for cycle in cycles:
        members = set(cycle)
        for source, target in list(arc):
            if source not in members or target not in members:
                continue
            if source < target:
                dropped.add((target, source))
            else:
                dropped.add((source, target))
    return {edge for edge in dropped if edge in arc}


def _fault_tree(traces: list[dict], bayesian: dict) -> dict:
    if not traces:
        return _step(
            5,
            TITLES[4],
            "There is no cause to place on a fault tree. An OR gate would use a noisy-or, and an AND gate would multiply the hop.",
            [],
            top=None,
        )
    lead = traces[0]
    beliefs = bayesian["beliefs"]
    cause_weights = [float(cause["weight"]) for cause in lead.get("causes") or []]
    gate = _combine(cause_weights) if cause_weights else 0.0
    rows = [
        {
            "title": f"OR gate · {lead['origin']}",
            "detail": (
                f"noisy-or {gate:.4f} of {len(cause_weights)} causes. "
                "The cause weights are not added. Shared causes are separated in the next step."
            ),
        }
    ]
    kept = []
    for row in lead.get("affected") or []:
        if row["depth"] == 0:
            continue
        belief = float(beliefs.get(row["module"], 0.0))
        if belief < MIN_RISK:
            continue
        chain = row.get("path") or []
        parent = chain[-2] if len(chain) >= 2 else lead["origin"]
        kept.append((row["depth"], belief, row["module"], parent, chain))
    kept.sort(key=lambda item: (item[0], -item[1], item[2]))
    for depth, belief, module, parent, chain in kept[:4]:
        rows.append(
            {
                "title": f"AND gate · {module}",
                "detail": (
                    f"continues only if {parent} carries the cause and {module} imports it · "
                    f"belief {belief:.4f} · depth {depth}"
                ),
            }
        )
    top = max(kept, key=lambda item: (item[0], item[1])) if kept else None
    if top is None:
        summary = (
            f"The OR gate at {lead['origin']} is a noisy-or of its causes. "
            "No importer stays above the cutoff, so the cascade stops at the origin."
        )
        top_module = lead["origin"]
    else:
        top_module = top[2]
        path = " → ".join(top[4])
        rows.append({"title": f"Top event · {top_module}", "detail": path})
        summary = (
            f"The OR gate at {lead['origin']} combines causes with a noisy-or. "
            f"Each hop is an AND of the parent cause and the import. The cascade reaches {top_module}."
        )
    return _step(5, TITLES[4], summary, rows, top=top_module, gate=gate)


def _correlate(traces: list[dict], cycles: list[list[str]], view: dict) -> dict:
    signals = _signals_at(view, 0.0)
    best: dict[tuple[str, str], dict] = {}
    family_rows = []
    seen_families = set()
    for trace in traces:
        families = _families(trace, cycles, signals)
        for family in families:
            if family["key"] not in seen_families:
                seen_families.add(family["key"])
                family_rows.append(family)
            for row in trace.get("affected") or []:
                carried = round(family["weight"] * (_hop(family["kind"]) ** row["depth"]), 4)
                key = (row["module"], family["key"])
                current = best.get(key)
                if current is None or carried > current["index"]:
                    best[key] = {"index": carried, "depth": row["depth"]}
    parts: dict[str, list[float]] = defaultdict(list)
    for (module, _key), value in best.items():
        parts[module].append(value["index"])
    indexes = {module: _combine(values) for module, values in parts.items()}
    naive = {}
    naive_sum = {}
    for module in indexes:
        values = []
        total = 0.0
        for trace in traces:
            match = next((row for row in trace.get("affected") or [] if row["module"] == module), None)
            if match is None:
                continue
            values.append(float(match["severity_score"]))
            total += float(match["severity_score"])
        naive[module] = _combine(values) if values else 0.0
        naive_sum[module] = round(total, 4)
    rows = []
    for family in family_rows:
        if not family["conditions"]:
            continue
        conditions = "; ".join(family["conditions"])
        rows.append(
            {
                "title": family["key"],
                "detail": f"{family['label']} is the common cause. Conditions, not extra indexes: {conditions}.",
            }
        )
    added = {module: round(sum(values), 4) for module, values in parts.items()}
    spread = sorted(indexes, key=lambda item: (-(added[item] - indexes[item]), item))
    for module in spread[:4]:
        rows.append(
            {
                "title": module,
                "detail": (
                    f"common-cause index {indexes[module]:.4f} · "
                    f"adding these families would be {added[module]:.4f}"
                ),
            }
        )
    shared = [family for family in family_rows if family["conditions"]]
    if shared:
        names = ", ".join(sorted({family["key"] for family in shared}))
        summary = (
            f"Shared causes count once: {names}. "
            "A module reached by several traces keeps a noisy-or of the remaining families. "
            "Those indexes are not added."
        )
    elif indexes:
        summary = (
            "No package or cycle is shared by two signals. "
            "Where traces meet, the index is still a noisy-or of the families, not a sum."
        )
    else:
        summary = "There is no cause to group. Shared causes would count once, and meeting traces would use a noisy-or."
    return _step(
        6,
        TITLES[5],
        summary,
        rows[:8],
        indexes=indexes,
        naive=naive,
        naive_sum=naive_sum,
        families=family_rows,
        best=[
            {"module": module, "family": key, "index": value["index"], "depth": value["depth"]}
            for (module, key), value in sorted(best.items())
        ],
    )


def _families(trace: dict, cycles: list[list[str]], signals: dict[str, list[dict]]) -> list[dict]:
    families = []
    packages: dict[str, dict] = {}
    for cause in trace.get("causes") or []:
        kind = cause.get("kind")
        label = str(cause.get("label") or "")
        weight = float(cause.get("weight") or 0)
        if kind in {"failure", "dependency_change"}:
            name = _package_from_label(label)
            if not name:
                continue
            bucket = packages.setdefault(name, {"weight": 0.0, "label": "", "kind": kind, "conditions": []})
            _keep_strongest(bucket, weight, label, kind)
        elif kind == "vulnerability":
            families.append(
                {
                    "key": f"vulnerability:{trace['origin']}:{label}",
                    "kind": "vulnerability",
                    "weight": weight,
                    "label": label,
                    "origin": trace["origin"],
                    "conditions": [],
                }
            )
        elif kind == "architecture":
            if "cycle" in label.lower():
                key = _cycle_key(trace["origin"], cycles)
            else:
                key = f"fanin:{trace['origin']}"
            families.append(
                {
                    "key": key,
                    "kind": "architecture",
                    "weight": weight,
                    "label": label,
                    "origin": trace["origin"],
                    "conditions": [],
                }
            )
    for name, bucket in packages.items():
        for signal in signals.get(name) or []:
            if _same_text(signal["label"], bucket["label"]) or any(_same_text(signal["label"], item) for item in bucket["conditions"]):
                continue
            if signal["weight"] > bucket["weight"]:
                if bucket["label"]:
                    bucket["conditions"].append(bucket["label"])
                bucket["weight"] = signal["weight"]
                bucket["label"] = signal["label"]
                bucket["kind"] = _signal_kind(signal["key"])
            else:
                bucket["conditions"].append(signal["label"])
        families.append(
            {
                "key": f"package:{name}",
                "kind": bucket["kind"],
                "weight": bucket["weight"],
                "label": bucket["label"],
                "origin": trace["origin"],
                "conditions": bucket["conditions"],
            }
        )
    return families


def _keep_strongest(bucket: dict, weight: float, label: str, kind: str) -> None:
    if weight > bucket["weight"]:
        if bucket["label"]:
            bucket["conditions"].append(bucket["label"])
        bucket["weight"] = weight
        bucket["label"] = label
        bucket["kind"] = kind
    elif label and label not in bucket["conditions"]:
        bucket["conditions"].append(label)


def _same_text(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left in right or right in left


def _signal_kind(key: str) -> str:
    if key == "failure":
        return "failure"
    return "dependency_change"


def _package_from_label(label: str) -> str:
    marker = " through "
    if marker not in label:
        return ""
    return label.rsplit(marker, 1)[1].strip().rstrip(".")


def _cycle_key(path: str, cycles: list[list[str]]) -> str:
    for cycle in cycles:
        if path in cycle:
            return "cycle:" + "|".join(cycle)
    return f"cycle:{path}"


def _signals_at(view: dict, as_of: float) -> dict[str, list[dict]]:
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    signals: dict[str, list[dict]] = {}

    def add(name: str, key: str, weight: float, label: str) -> None:
        if not name:
            return
        bucket = signals.setdefault(name, [])
        if any(item["label"] == label for item in bucket):
            return
        bucket.append({"key": key, "weight": weight, "label": label})

    for fact in facts:
        if fact.get("predicate") != "attributed_to" or fact.get("kind") != "event":
            continue
        if not holds(fact, as_of):
            continue
        age = fact.get("at_age")
        when = "in history" if age is None else f"{_age(age)}d ago"
        add(_pkg_name(str(fact.get("object") or "")), "failure", FAILURE, f"Build failure {when}")
    for fact in facts:
        if fact.get("predicate") != "depends_on" or not holds(fact, as_of):
            continue
        name = _pkg_name(str(fact.get("object") or ""))
        opened = float(fact.get("from_age") or 0)
        if 0 < opened <= 90:
            add(name, "upgrade", UPGRADE, f"Upgraded {_age(opened)}d ago")
    for fact in facts:
        if fact.get("predicate") != "plugin" or not holds(fact, as_of):
            continue
        add(str(fact.get("subject") or ""), "plugin", PLUGIN, f"Binds {fact.get('object')}")
    return signals


def _dynamic(view: dict, correlated: dict) -> dict:
    past_signals = _signals_at(view, PAST_DAYS)
    now = correlated["indexes"]
    past = _replay(correlated, past_signals)
    focus = max(now, key=lambda item: (now[item], item)) if now else ""
    now_index = now.get(focus, 0.0)
    past_index = past.get(focus, 0.0)
    if not focus:
        summary = "There is no index to replay. Package facts would be read again 20 days ago, and the package index would stay."
        rows = []
    elif abs(now_index - past_index) < 1e-9:
        summary = (
            f"{focus} holds at {now_index:.4f} now and 20d ago. "
            "Package facts that still hold do not move it. The package index on the page is not rewritten."
        )
        rows = [{"title": focus, "detail": f"now {now_index:.4f} · 20d ago {past_index:.4f}"}]
    else:
        summary = (
            f"{focus} is {now_index:.4f} now and {past_index:.4f} at 20d ago. "
            "Only package failure, upgrade, and plugin facts move. Code findings stay. The package index on the page is not rewritten."
        )
        rows = [
            {
                "title": focus,
                "detail": f"now {now_index:.4f} · 20d ago {past_index:.4f} · package facts inside the 20-day window changed",
            }
        ]
    return _step(7, TITLES[6], summary, rows, now=now_index, past=past_index, focus=focus, past_indexes=past)


def _replay(correlated: dict, signals: dict) -> dict[str, float]:
    """Replay keeps every family and replaces a package weight with the facts that held that day."""
    package_weights = {}
    for family in correlated["families"]:
        if not str(family["key"]).startswith("package:"):
            continue
        name = family["key"].split(":", 1)[1]
        package_weights[family["key"]] = _lead_signal(signals.get(name) or [])
    best: dict[tuple[str, str], float] = {}
    for item in correlated["best"]:
        family_key = item["family"]
        if family_key in package_weights:
            lead = package_weights[family_key]
            if lead is None:
                continue
            carried = round(lead["weight"] * (_hop(lead["kind"]) ** item["depth"]), 4)
        else:
            carried = item["index"]
        key = (item["module"], family_key)
        if carried > best.get(key, -1.0):
            best[key] = carried
    parts: dict[str, list[float]] = defaultdict(list)
    for (module, _key), value in best.items():
        if value > 0:
            parts[module].append(value)
    return {module: _combine(values) for module, values in parts.items()}


def _lead_signal(signals: list[dict]) -> dict | None:
    if not signals:
        return None
    lead = max(signals, key=lambda item: (item["weight"], item["key"]))
    return {"weight": lead["weight"], "kind": _signal_kind(lead["key"]), "label": lead["label"]}


def _contain(correlated: dict, by_path: dict, dynamic: dict) -> dict:
    indexes = correlated["indexes"]
    if not indexes:
        return _step(
            8,
            TITLES[7],
            "There is no downstream index to cut. One action would be tried, and the repository would not be edited.",
            [],
            chosen="",
            drop=0.0,
        )
    reached = _reached(correlated)
    base = _objective(indexes, by_path, reached)
    candidates = []
    for family in correlated["families"]:
        reduced = _without(correlated, {family["key"]})
        score = _objective(reduced, by_path, reached)
        candidates.append(
            {
                "title": f"Remove {family['label']} at {family['origin']}",
                "detail": f"drops the downstream index from {base:.4f} to {score:.4f}",
                "drop": round(base - score, 4),
                "score": score,
            }
        )
    withheld = _withhold_upgrade(correlated, by_path, base)
    if withheld is not None:
        candidates.append(withheld)
    covered = _cover_test(correlated, by_path, base)
    if covered is not None:
        candidates.append(covered)
    candidates.sort(key=lambda item: (-item["drop"], item["title"]))
    chosen = candidates[0]
    rows = [{"title": item["title"], "detail": item["detail"]} for item in candidates if item["drop"] > 0][:3]
    if withheld is not None:
        rows.append({"title": withheld["title"], "detail": withheld["detail"]})
    summary = (
        f"One cut: {chosen['title']}. The downstream index moves from {base:.4f} to {chosen['score']:.4f}. "
        "Actions are not stacked. The repository is not edited."
    )
    if dynamic.get("focus") and abs(float(dynamic.get("now") or 0) - float(dynamic.get("past") or 0)) > 1e-9:
        summary += " The cut uses the index now, not the replay from 20d ago."
    return _step(8, TITLES[7], summary, rows, chosen=chosen["title"], drop=chosen["drop"], before=base, after=chosen["score"])


def _objective(indexes: dict[str, float], by_path: dict, reached: set[str]) -> float:
    downstream = [
        round(indexes[module] * _evidence(by_path.get(module)), 4)
        for module in reached
        if module in indexes
    ]
    if not downstream:
        return 0.0
    return max(downstream)


def _reached(correlated: dict) -> set[str]:
    return {item["module"] for item in correlated["best"] if item["depth"] > 0}


def _without(correlated: dict, keys: set[str]) -> dict[str, float]:
    parts: dict[str, list[float]] = defaultdict(list)
    for item in correlated["best"]:
        if item["family"] in keys:
            continue
        parts[item["module"]].append(item["index"])
    return {module: _combine(values) for module, values in parts.items() if values}


def _withhold_upgrade(correlated: dict, by_path: dict, base: float) -> dict | None:
    package = next((family for family in correlated["families"] if str(family["key"]).startswith("package:")), None)
    if package is None:
        return None
    name = package["key"].split(":", 1)[1]
    upgrade = next((label for label in package["conditions"] if label.startswith("Upgraded")), None)
    if upgrade is None and not str(package["label"]).startswith("Upgraded"):
        return None
    score = _objective(correlated["indexes"], by_path, _reached(correlated))
    return {
        "title": f"Withhold the {name} upgrade",
        "detail": (
            f"The upgrade is a condition of the same package cause. "
            f"Withholding it alone does not move the index while a stronger {name} signal remains. "
            f"Downstream index stays {score:.4f}."
        ),
        "drop": 0.0 if abs(score - base) < 1e-9 else round(base - score, 4),
        "score": score,
    }


def _cover_test(correlated: dict, by_path: dict, base: float) -> dict | None:
    reached = _reached(correlated)
    ranked = []
    for module in reached:
        if module not in correlated["indexes"]:
            continue
        module_row = by_path.get(module)
        if module_row is None or float(module_row.get("test_gap") or 0) < 0.5:
            continue
        ranked.append((correlated["indexes"][module] * _evidence(module_row), module))
    if not ranked:
        return None
    _score, module = max(ranked)
    tested = round(correlated["indexes"][module] * 0.45, 4)
    untested = round(correlated["indexes"][module] * _evidence(by_path.get(module)), 4)
    others = [
        round(correlated["indexes"][item] * _evidence(by_path.get(item)), 4)
        for item in reached
        if item != module and item in correlated["indexes"]
    ]
    after = max([tested, *others], default=tested)
    return {
        "title": f"Cover {module} with a test",
        "detail": f"drops that file's conditional index from {untested:.4f} to {tested:.4f}",
        "drop": round(max(0.0, base - after), 4),
        "score": after,
    }


def _uncertainty(view: dict, correlated: dict, probabilistic: dict) -> dict:
    indexes = correlated["indexes"]
    if not indexes:
        return _step(
            9,
            TITLES[8],
            "There is no common-cause index to bound. The band would not be a probability.",
            [],
            point=None,
            low=None,
            high=None,
            width=None,
        )
    module = max(indexes, key=lambda item: (indexes[item], item))
    point = indexes[module]
    shared = any(family["conditions"] for family in correlated["families"])
    deep = any(item["depth"] >= 2 for item in probabilistic.get("rows_by_module") or [])
    gap = float((view and next((row.get("test_gap") or 0 for row in (view.get("modules") or []) if row.get("path") == module), 0)) or 0)
    outcomes = len(view.get("outcomes") or [])
    observed = any(str(family["key"]).startswith("vulnerability:") and family["origin"] == module for family in correlated["families"])
    width = 0.08
    parts = ["base 0.08"]
    if shared:
        width += 0.04
        parts.append("shared cause +0.04")
    if deep:
        width += 0.04
        parts.append("a hop reaches depth 2 +0.04")
    if gap >= 0.5:
        width += 0.04
        parts.append("untested +0.04")
    if outcomes < 3:
        width += 0.04
        parts.append("fewer than 3 outcomes +0.04")
    if observed:
        width -= 0.02
        parts.append("observed in the file -0.02")
    width = round(min(0.24, max(0.06, width)), 4)
    low = round(max(0.0, point - width), 4)
    high = round(min(1.0, point + width), 4)
    if width <= 0.08:
        confidence = "high"
    elif width <= 0.12:
        confidence = "medium"
    else:
        confidence = "low"
    summary = (
        f"The band around {module} is {low:.2f}–{high:.2f} on the common-cause index {point:.4f}. "
        "It is not a probability."
    )
    rows = [{"title": module, "detail": f"index {point:.4f} · band {low:.2f}–{high:.2f} · width {width:.2f} · {confidence} · " + " · ".join(parts)}]
    return _step(9, TITLES[8], summary, rows, point=point, low=low, high=high, width=width, module=module)


def _calibrate(view: dict, report: dict, correlated: dict) -> dict:
    indexes = correlated["indexes"]
    outcomes = [item for item in view.get("outcomes") or [] if item.get("module") in indexes]
    excluded = _excluded_failures(view, correlated)
    packages = report.get("packages") or []
    lead = packages[0]["risk"] if packages else None
    if not outcomes:
        reading = "uncalibrated"
        gap = None
        summary = (
            "The propagation index is uncalibrated: no recorded outcome sits on a reached module. "
            "A build failure used as a cause is evidence, not a prediction. The package index is not adjusted."
        )
    else:
        reading = "compared"
        gap = round(
            sum(float(indexes.get(item["module"]) or 0) - (1.0 if item.get("happened") else 0.0) for item in outcomes)
            / len(outcomes),
            4,
        )
        summary = (
            f"Across {len(outcomes)} outcomes on reached modules, the common-cause index differs by {gap:.2f}. "
            "That gap does not rewrite the package index."
        )
    rows = [{"title": reading, "detail": summary}]
    for item in excluded[:2]:
        rows.append({"title": item["package"], "detail": item["reason"]})
    return _step(
        10,
        TITLES[9],
        summary,
        rows,
        reading=reading,
        gap=gap,
        excluded=excluded,
        package_risk=lead,
        n=len(outcomes),
    )


def _excluded_failures(view: dict, correlated: dict) -> list[dict]:
    packages = {
        family["key"].split(":", 1)[1]
        for family in correlated["families"]
        if str(family["key"]).startswith("package:")
    }
    rows = []
    facts = ((view.get("dynamics") or {}).get("facts")) or []
    for fact in facts:
        if fact.get("predicate") != "attributed_to" or fact.get("kind") != "event":
            continue
        name = _pkg_name(str(fact.get("object") or ""))
        if name not in packages:
            continue
        rows.append(
            {
                "package": name,
                "reason": f"The build failure attributed to {name} is a cause of the trace, so it is not scored as a prediction.",
            }
        )
    return rows


def _age(value) -> str:
    number = float(value or 0)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")
