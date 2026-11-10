"""Assemble the twin document the API and the UI both read."""

from __future__ import annotations

from collections import defaultdict

from app.accuracy import evaluate
from app.agents import ROSTER, analyze, coordinate
from app.counterfactual import simulate
from app.engine import decide
from app.failures import predict
from app.forecast import build_forecast
from app.propagate import propagate
from app.score import (
    FEATURE_ORDER,
    KNEES,
    LABELS,
    code_edges,
    merge_models,
    metrics,
    observables_from_raw,
    score_observables,
)
from app.settings import CHURN_WINDOW_DAYS, HALF_LIFE_DAYS, HISTORY_WINDOW_DAYS, LEARNING_RATE
from app.simulate import spread
from app.temporal import build_dynamics


def build_twin(
    raw: dict,
    models: dict | None = None,
    outcomes: list[dict] | None = None,
    history: list[dict] | None = None,
) -> dict:
    active = merge_models(models)
    observables = observables_from_raw(raw)
    modules = score_observables(observables, raw["edges"], active)
    edges = _view_edges(modules, raw["edges"])
    external = {name for module in raw["modules"] for name in module.get("external_imports", [])}
    declared = set(raw.get("dependencies") or [])
    context = {
        "signals": raw.get("signals") or {},
        "dependencies": sorted(declared),
        "unused": sorted(declared - external),
        "undeclared": sorted(external - declared),
    }
    findings = analyze(modules, edges, context)
    plan = coordinate(findings)
    risks = _risks(modules)
    health = _health(modules, findings)
    totals = metrics(modules, edges)
    document = {
        "project": {
            "name": raw["name"],
            "root": raw["root"],
            "description": raw.get("description") or "",
            "scanned_at": raw["scanned_at"],
            "warnings": raw.get("warnings") or [],
            "dependencies": raw.get("dependencies") or [],
        },
        "health": health,
        "narrative": _narrate(raw, modules, risks, findings, health),
        "kpis": _kpis(modules, edges, findings, totals),
        "services": _services(modules),
        "modules": modules,
        "edges": edges,
        "risks": risks,
        "findings": findings,
        "plan": plan,
        "roster": [
            {
                "name": name,
                "mandate": mandate,
                "open": sum(1 for finding in findings if finding["agent"] == name),
            }
            for name, mandate in ROSTER
        ],
        "timeline": _timeline(raw.get("events") or []),
        "dynamics": build_dynamics(raw, active),
        "models": active,
        "model": {
            "name": "explainable logistic twin",
            "learning_rate": LEARNING_RATE,
            "features": [{"key": key, "label": LABELS[key]} for key in FEATURE_ORDER],
            "knees": KNEES,
            "windows": {
                "churn_days": CHURN_WINDOW_DAYS,
                "history_days": HISTORY_WINDOW_DAYS,
                "half_life_days": HALF_LIFE_DAYS,
            },
        },
        "weight_history": list(history or []),
        "outcomes": list(outcomes or []),
        "calibration": _calibration(outcomes or []),
        "forecast": build_forecast(modules, spread(modules), len(outcomes or [])),
        "signals": raw.get("signals") or {},
    }
    document["dependencies"] = propagate(document)
    document["engineering_failures"] = predict(document)
    document["counterfactual"] = simulate(document)
    document["accuracy"] = evaluate(document, raw)
    document["engine"] = decide(document, document["accuracy"])
    return document


def _view_edges(modules: list[dict], edges: list[dict]) -> list[dict]:
    services = {module["path"]: module["service"] for module in modules}
    view = []
    for edge in code_edges(modules, edges):
        view.append(
            {
                "source": edge["source"],
                "target": edge["target"],
                "cross": services.get(edge["source"]) != services.get(edge["target"]),
            }
        )
    view.sort(key=lambda edge: (edge["source"], edge["target"]))
    return view


def _risks(modules: list[dict]) -> list[dict]:
    rows = []
    for module in modules:
        if module["role"] != "code":
            continue
        for kind in ("bug", "incident", "delay", "perf"):
            contributions = module["contributions"].get(kind) or []
            drivers = [row["label"] for row in contributions if row["contribution"] > 0.02][:3]
            if not drivers and contributions:
                drivers = [contributions[0]["label"]]
            rows.append(
                {
                    "module": module["path"],
                    "service": module["service"],
                    "kind": kind,
                    "score": module["risks"][kind],
                    "drivers": drivers,
                }
            )
    rows.sort(key=lambda row: (-row["score"], row["kind"], row["module"]))
    return rows


def _services(modules: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for module in modules:
        if module["role"] == "code":
            grouped[module["service"]].append(module)
    services = []
    for name, group in grouped.items():
        authors = sorted({author for module in group for author in module["authors"]})
        services.append(
            {
                "name": name,
                "modules": len(group),
                "loc": sum(module["loc"] for module in group),
                "authors": authors,
                "bus_factor": len(authors),
                "bug": round(max(module["risks"]["bug"] for module in group), 4),
                "incident": round(max(module["risks"]["incident"] for module in group), 4),
                "debt": round(sum(module["debt"] for module in group) / len(group), 4),
                "churn": round(sum(module["churn"] for module in group), 1),
            }
        )
    services.sort(key=lambda service: service["bug"], reverse=True)
    return services


def _kpis(modules: list[dict], edges: list[dict], findings: list[dict], totals: dict) -> dict:
    code = [module for module in modules if module["role"] == "code"]
    bus_pool = []
    by_service: dict[str, list[dict]] = defaultdict(list)
    for module in code:
        by_service[module["service"]].append(module)
    for group in by_service.values():
        churn = sum(module["churn"] for module in group)
        authors = {author for module in group for author in module["authors"]}
        if churn >= 80 and authors:
            bus_pool.append(len(authors))
    return {
        "modules": len(code),
        "services": len(by_service),
        "edges": len(edges),
        "loc": sum(module["loc"] for module in code),
        "bug": round(max((module["risks"]["bug"] for module in code), default=0.0), 4),
        "incident": round(max((module["risks"]["incident"] for module in code), default=0.0), 4),
        "delay": totals["delay"],
        "perf": totals["perf"],
        "debt": totals["debt"],
        "bus_factor": min(bus_pool) if bus_pool else 0,
        "critical": sum(1 for finding in findings if finding["severity"] == "critical"),
        "expected_bug_modules": totals["expected_bug_modules"],
        "expected_incident_services": totals["expected_incident_services"],
        "cycles": totals["cycles"],
        "spread": spread(modules),
    }


def _health(modules: list[dict], findings: list[dict]) -> int:
    code = [module for module in modules if module["role"] == "code"]
    if not code:
        return 100
    bugs = sorted((module["risks"]["bug"] for module in code), reverse=True)
    top = sum(bugs[:3]) / min(3, len(bugs))
    incident = max(module["risks"]["incident"] for module in code)
    delay = sum(module["risks"]["delay"] for module in code) / len(code)
    debt = sum(module["debt"] for module in code) / len(code)
    critical = 1 if any(finding["severity"] == "critical" for finding in findings) else 0
    highs = sum(1 for finding in findings if finding["severity"] == "high")
    penalty = 32 * top + 18 * incident + 10 * delay + 10 * debt + 14 * critical + min(highs, 6) * 1.4
    return int(round(max(0.0, min(100.0, 100 - penalty))))


def _narrate(raw: dict, modules: list[dict], risks: list[dict], findings: list[dict], health: int) -> str:
    code = [module for module in modules if module["role"] == "code"]
    services = {module["service"] for module in code}
    bugs = [risk for risk in risks if risk["kind"] == "bug"]
    if not bugs:
        return f"{raw['name']} has no code modules in this scan."
    top = bugs[0]
    drivers = ", ".join(top["drivers"][:3]) or "the current feature mix"
    critical = sum(1 for finding in findings if finding["severity"] == "critical")
    return (
        f"{raw['name']} currently spans {len(code)} code modules in {len(services)} services. "
        f"Health is {health}. The sharpest bug risk is {top['module']} at {top['score']:.2f}, "
        f"driven by {drivers}. The council is holding {critical} critical findings. "
        "Scores are explainable indexes: compare them, simulate a change, then record what actually happened."
    )


def _timeline(events: list[dict]) -> list[dict]:
    ordered = sorted(events, key=lambda event: float(event.get("age_days", 0)))
    rows = []
    for event in ordered[:12]:
        rows.append(
            {
                "age_days": round(float(event.get("age_days", 0)), 2),
                "type": event.get("type"),
                "summary": event.get("summary"),
                "service": event.get("service"),
            }
        )
    return rows


def _calibration(outcomes: list[dict]) -> dict:
    if not outcomes:
        return {"n": 0, "brier": None, "confirmed": 0}
    total = 0.0
    confirmed = 0
    for outcome in outcomes:
        happened = 1.0 if outcome.get("happened") else 0.0
        predicted = float(outcome.get("predicted") or 0.0)
        total += (predicted - happened) ** 2
        confirmed += int(happened)
    return {
        "n": len(outcomes),
        "brier": round(total / len(outcomes), 4),
        "confirmed": confirmed,
    }
