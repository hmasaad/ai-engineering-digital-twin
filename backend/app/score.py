"""Explainable risk indexes.

Each feature is an absolute saturation curve, not a rank within the repo, so a
scenario that removes churn or incidents actually moves the score. Four logistic
models (bug, incident, delay, performance) sit on the same features. Debt is a
fixed weighted index and does not learn.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict

from app.settings import CHURN_WINDOW_DAYS, HALF_LIFE_DAYS, HISTORY_WINDOW_DAYS, LEARNING_RATE

FEATURE_ORDER = [
    "churn",
    "complexity",
    "coupling",
    "test_pressure",
    "todo_density",
    "incident_pressure",
    "owner_risk",
    "instability",
    "perf_smell",
    "intervention_drag",
]

LABELS = {
    "churn": "recent churn",
    "complexity": "branch complexity",
    "coupling": "import coupling",
    "test_pressure": "thin tests on active code",
    "todo_density": "unfinished markers",
    "incident_pressure": "recent incidents",
    "owner_risk": "narrow ownership",
    "instability": "rollbacks and slow review",
    "perf_smell": "performance smells",
    "intervention_drag": "scenario drag",
    "bias": "baseline",
}

# Raw value that maps to about 0.63. Documented in the product so scores stay legible.
KNEES = {
    "churn": 400.0,
    "complexity": 12.0,
    "coupling": 4.0,
    "todo_density": 3.0,
    "incident_pressure": 0.7,
    "instability": 1.0,
    "perf_smell": 2.0,
}

SEVERITY_WEIGHT = {"sev1": 1.0, "sev2": 0.6, "sev3": 0.3, "sev4": 0.15}
SEVERITY_RANK = {"sev1": 4, "sev2": 3, "sev3": 2, "sev4": 1}
PERF_KINDS = {"unbounded-loop", "blocking-sleep", "select-star"}

DEBT_WEIGHTS = {
    "complexity": 0.30,
    "test_pressure": 0.24,
    "todo_density": 0.16,
    "coupling": 0.18,
    "churn": 0.12,
}

DEFAULT_MODELS = {
    "bug": {
        "bias": -2.15,
        "weights": {
            "churn": 1.15,
            "complexity": 0.72,
            "coupling": 0.38,
            "test_pressure": 0.95,
            "todo_density": 0.28,
            "incident_pressure": 1.05,
            "owner_risk": 0.22,
            "instability": 0.58,
            "perf_smell": 0.18,
            "intervention_drag": 0.0,
        },
    },
    "incident": {
        "bias": -2.4,
        "weights": {
            "churn": 0.35,
            "complexity": 0.15,
            "coupling": 0.45,
            "test_pressure": 0.40,
            "todo_density": 0.05,
            "incident_pressure": 1.70,
            "owner_risk": 0.15,
            "instability": 0.85,
            "perf_smell": 0.55,
            "intervention_drag": 0.0,
        },
    },
    "delay": {
        "bias": -1.85,
        "weights": {
            "churn": 0.70,
            "complexity": 0.25,
            "coupling": 0.55,
            "test_pressure": 0.30,
            "todo_density": 0.10,
            "incident_pressure": 0.25,
            "owner_risk": 0.85,
            "instability": 0.90,
            "perf_smell": 0.05,
            "intervention_drag": 1.15,
        },
    },
    "perf": {
        "bias": -2.30,
        "weights": {
            "churn": 0.15,
            "complexity": 0.85,
            "coupling": 0.25,
            "test_pressure": 0.15,
            "todo_density": 0.05,
            "incident_pressure": 0.35,
            "owner_risk": 0.05,
            "instability": 0.20,
            "perf_smell": 1.45,
            "intervention_drag": 0.0,
        },
    },
}


def copy_models() -> dict:
    return copy.deepcopy(DEFAULT_MODELS)


def merge_models(saved: dict | None) -> dict:
    models = copy_models()
    if not saved:
        return models
    for kind, model in models.items():
        incoming = saved.get(kind) or {}
        if "bias" in incoming:
            model["bias"] = float(incoming["bias"])
        for key, value in (incoming.get("weights") or {}).items():
            if key in model["weights"]:
                model["weights"][key] = float(value)
    return models


def sigmoid(value: float) -> float:
    if value > 12:
        return 1.0
    if value < -12:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def learn(
    weights: dict[str, float],
    bias: float,
    features: dict[str, float],
    outcome: float,
    lr: float = LEARNING_RATE,
) -> tuple[dict[str, float], float, float, float]:
    """One logistic step. Returns new weights, new bias, prediction, and error."""
    prediction = sigmoid(bias + sum(weights[key] * float(features.get(key, 0.0)) for key in weights))
    error = float(outcome) - prediction
    updated = {
        key: _clamp(weights[key] + lr * error * float(features.get(key, 0.0)), -0.25, 3.8)
        for key in weights
    }
    return updated, _clamp(bias + lr * error, -4.5, 1.5), prediction, error


def observables_from_raw(raw: dict) -> list[dict]:
    tested = _tested_paths(raw)
    per_file, per_service = _history(raw.get("events") or [])
    observables = []
    for module in raw["modules"]:
        path = module["path"]
        service = module["service"]
        incident = per_service["incident"].get(service, {})
        observables.append(
            {
                "path": path,
                "language": module["language"],
                "service": service,
                "layer": module["layer"],
                "role": module["role"],
                "loc": module["loc"],
                "complexity": module["complexity"],
                "todos": module["todos"],
                "churn": per_file["churn"].get(path, 0.0),
                "authors": sorted(per_file["authors"].get(path, set())),
                "review_hours": per_file["review"].get(path, 0.0),
                "incident_raw": incident.get("raw", 0.0),
                "incident_level": incident.get("level"),
                "incident_summary": incident.get("summary"),
                "rollbacks": per_service["rollback"].get(service, 0.0),
                "perf_smells": float(sum(1 for item in module["evidence"] if item["kind"] in PERF_KINDS)),
                "test_gap": 0.0 if module["role"] != "code" or path in tested else 1.0,
                "intervention_drag": 0.0,
                "evidence": list(module["evidence"]),
                "imports": list(module["imports"]),
                "external_imports": list(module.get("external_imports") or []),
            }
        )
    return observables


def score_observables(observables: list[dict], edges: list[dict], models: dict) -> list[dict]:
    roles = {item["path"]: item["role"] for item in observables}
    code_edges = [
        edge
        for edge in edges
        if roles.get(edge["source"]) == "code" and roles.get(edge["target"]) == "code"
    ]
    degree: dict[str, int] = defaultdict(int)
    importers: dict[str, list[str]] = defaultdict(list)
    imports: dict[str, list[str]] = defaultdict(list)
    for edge in code_edges:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1
        importers[edge["target"]].append(edge["source"])
        imports[edge["source"]].append(edge["target"])

    views = []
    for obs in observables:
        features = _features(obs, degree[obs["path"]]) if obs["role"] == "code" else {}
        risks = {}
        contributions = {}
        if features:
            for kind, model in models.items():
                risks[kind] = round(_score(features, model), 4)
                contributions[kind] = _contributions(features, model)
            debt = round(sum(DEBT_WEIGHTS[key] * features.get(key, 0.0) for key in DEBT_WEIGHTS), 4)
        else:
            risks = {kind: 0.0 for kind in models}
            debt = 0.0
        views.append(
            {
                "path": obs["path"],
                "language": obs["language"],
                "service": obs["service"],
                "layer": obs["layer"],
                "role": obs["role"],
                "loc": obs["loc"],
                "complexity": obs["complexity"],
                "todos": obs["todos"],
                "churn": round(obs["churn"], 2),
                "authors": list(obs["authors"]),
                "review_hours": obs["review_hours"],
                "incident_raw": round(obs["incident_raw"], 4),
                "incident_level": obs["incident_level"],
                "incident_summary": obs["incident_summary"],
                "rollbacks": round(obs["rollbacks"], 4),
                "test_gap": obs["test_gap"],
                "perf_smells": obs["perf_smells"],
                "features": {key: round(value, 6) for key, value in features.items()},
                "contributions": contributions,
                "risks": risks,
                "debt": debt,
                "debt_parts": {
                    key: round(DEBT_WEIGHTS[key] * features.get(key, 0.0), 4) for key in DEBT_WEIGHTS
                }
                if features
                else {},
                "evidence": obs["evidence"],
                "imports": sorted(imports.get(obs["path"], [])),
                "imported_by": sorted(importers.get(obs["path"], [])),
                "external_imports": list(obs.get("external_imports") or []),
            }
        )
    return views


def code_edges(modules: list[dict], edges: list[dict]) -> list[dict]:
    code = {module["path"] for module in modules if module["role"] == "code"}
    return [edge for edge in edges if edge["source"] in code and edge["target"] in code]


def metrics(modules: list[dict], edges: list[dict]) -> dict:
    code = [module for module in modules if module["role"] == "code"]
    if not code:
        return {
            "expected_bug_modules": 0.0,
            "expected_incident_services": 0.0,
            "delay": 0.0,
            "perf": 0.0,
            "debt": 0.0,
            "cycles": 0,
        }
    by_service: dict[str, list[dict]] = defaultdict(list)
    for module in code:
        by_service[module["service"]].append(module)
    incidents = sum(max(module["risks"]["incident"] for module in group) for group in by_service.values())
    from app.graph import find_cycles

    return {
        "expected_bug_modules": round(sum(module["risks"]["bug"] for module in code), 4),
        "expected_incident_services": round(incidents, 4),
        "delay": round(sum(module["risks"]["delay"] for module in code) / len(code), 4),
        "perf": round(sum(module["risks"]["perf"] for module in code) / len(code), 4),
        "debt": round(sum(module["debt"] for module in code) / len(code), 4),
        "cycles": len(find_cycles(code_edges(modules, edges))),
    }


def _features(obs: dict, degree: int) -> dict[str, float]:
    features = {
        "churn": _sat(obs["churn"], KNEES["churn"]),
        "complexity": _sat(obs["complexity"], KNEES["complexity"]),
        "coupling": _sat(degree, KNEES["coupling"]),
        "todo_density": _sat(obs["todos"], KNEES["todo_density"]),
        "incident_pressure": _sat(obs["incident_raw"], KNEES["incident_pressure"]),
        "owner_risk": _owner_risk(obs["authors"]),
        "instability": _sat(obs["rollbacks"] + min(float(obs["review_hours"]), 168.0) / 72.0, KNEES["instability"]),
        "perf_smell": _sat(obs["perf_smells"], KNEES["perf_smell"]),
        "intervention_drag": max(0.0, min(1.0, float(obs["intervention_drag"]))),
    }
    features["test_pressure"] = float(obs["test_gap"]) * max(
        features["churn"],
        features["complexity"],
        features["incident_pressure"],
        0.12,
    )
    return features


def _owner_risk(authors: list[str]) -> float:
    count = len(authors)
    if count == 0:
        return 0.30
    if count == 1:
        return 1.0
    if count == 2:
        return 0.42
    return max(0.12, 1.0 / count)


def _contributions(features: dict[str, float], model: dict) -> list[dict]:
    rows = []
    for key, weight in model["weights"].items():
        value = float(features.get(key, 0.0))
        rows.append(
            {
                "feature": key,
                "label": LABELS[key],
                "value": round(value, 4),
                "weight": round(weight, 4),
                "contribution": round(value * weight, 4),
            }
        )
    rows.sort(key=lambda row: abs(row["contribution"]), reverse=True)
    return rows


def _score(features: dict[str, float], model: dict) -> float:
    total = model["bias"] + sum(weight * float(features.get(key, 0.0)) for key, weight in model["weights"].items())
    return sigmoid(total)


def _sat(value: float, knee: float) -> float:
    if value <= 0 or knee <= 0:
        return 0.0
    return 1.0 - math.exp(-value / knee)


def _decay(age: float) -> float:
    return 0.5 ** (age / HALF_LIFE_DAYS)


def _tested_paths(raw: dict) -> set[str]:
    roles = {module["path"]: module["role"] for module in raw["modules"]}
    return {edge["target"] for edge in raw["edges"] if roles.get(edge["source"]) == "test"}


def _history(events: list[dict]) -> tuple[dict, dict]:
    churn: dict[str, float] = defaultdict(float)
    authors: dict[str, set[str]] = defaultdict(set)
    review: dict[str, float] = defaultdict(float)
    incident: dict[str, dict] = {}
    rollback: dict[str, float] = defaultdict(float)

    for event in events:
        age = float(event.get("age_days", 0))
        if age < 0 or age > HISTORY_WINDOW_DAYS:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "commit":
            author = data.get("author")
            stats = data.get("file_stats") or {}
            for path, change in stats.items():
                if age <= CHURN_WINDOW_DAYS:
                    churn[path] += (int(change.get("add", 0)) + int(change.get("del", 0))) * _decay(age)
                if author:
                    authors[path].add(author)
            if author and not stats:
                for path in event.get("files") or []:
                    authors[path].add(author)
        elif kind == "review":
            hours = float(data.get("hours") or 0)
            for path in event.get("files") or []:
                review[path] = max(review[path], hours)
        elif kind == "incident" and event.get("service"):
            service = event["service"]
            severity = data.get("severity") or "sev3"
            current = incident.setdefault(
                service, {"raw": 0.0, "level": None, "summary": None, "rank": -1, "age": 10_000}
            )
            current["raw"] += SEVERITY_WEIGHT.get(severity, 0.4) * _decay(age)
            rank = SEVERITY_RANK.get(severity, 0)
            if age <= 120 and (rank > current["rank"] or (rank == current["rank"] and age < current["age"])):
                current["rank"] = rank
                current["age"] = age
                current["level"] = severity
                current["summary"] = event.get("summary")
        elif kind == "deploy" and event.get("service") and data.get("result") == "rollback":
            rollback[event["service"]] += _decay(age)

    return (
        {"churn": churn, "authors": authors, "review": review},
        {"incident": incident, "rollback": rollback},
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
