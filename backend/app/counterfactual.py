"""Contrary engineering worlds.

Each world removes one recorded fact, then dependency risk is propagated
again. Withholding an upgrade also removes a build failure that happened
after that upgrade. The bug index is not resimulated. A contrary world is
not evidence the other world would have shipped.
"""

from __future__ import annotations

from app.propagate import UPGRADE_WINDOW_DAYS, propagate
from app.temporal import holds


def simulate(view: dict) -> dict:
    facts = list(((view.get("dynamics") or {}).get("facts")) or [])
    base = view.get("dependencies") or {}
    packages = {item["name"]: item for item in base.get("packages") or []}
    cases = []
    for name, package in packages.items():
        keys = {signal["key"] for signal in package.get("signals") or []}
        if "upgrade" not in keys and "failure" not in keys:
            continue
        upgrade_age = _upgrade_age(facts, name)
        worlds = []
        if upgrade_age is not None:
            worlds.append(_world(view, base, name, "withhold_upgrade", upgrade_age))
        if "failure" in keys:
            worlds.append(_world(view, base, name, "withhold_failure", upgrade_age))
        if not worlds:
            continue
        factual = package["risk"]
        cases.append(
            {
                "package": name,
                "version": package.get("version") or "",
                "factual": {"risk": factual, "drivers": list(package.get("drivers") or [])},
                "worlds": worlds,
            }
        )
    cases.sort(key=lambda item: (-item["factual"]["risk"], item["package"]))
    return {
        "cases": cases[:3],
        "summary": _summary(cases),
        "note": (
            "Each contrary world removes one recorded engineering fact and propagates dependency risk again. "
            "Withholding an upgrade also removes a build failure that came after it. "
            "The bug index is not resimulated. A contrary world is not evidence the other world would have shipped."
        ),
    }


def _world(view: dict, base: dict, name: str, kind: str, upgrade_age: float | None) -> dict:
    facts = list(((view.get("dynamics") or {}).get("facts")) or [])
    kept = [fact for fact in facts if not _drops(fact, name, kind, upgrade_age)]
    report = propagate({**view, "dynamics": {**(view.get("dynamics") or {}), "facts": kept}})
    found = next((item for item in report["packages"] if item["name"] == name), None)
    risk = 0.0 if found is None else found["risk"]
    factual = next(item["risk"] for item in base.get("packages") or [] if item["name"] == name)
    before = {
        item["module"]: item
        for item in base.get("modules") or []
        if name in (item.get("packages") or []) or item.get("via") == name
    }
    after = {item["module"]: item for item in report.get("modules") or []}
    modules = []
    for path, item in before.items():
        landed = after.get(path)
        modules.append(
            {
                "module": path,
                "depth": item["depth"],
                "before": item["risk"],
                "after": 0.0 if landed is None else landed["risk"],
                "delta": round((0.0 if landed is None else landed["risk"]) - item["risk"], 4),
            }
        )
    modules.sort(key=lambda item: (item["delta"], item["depth"], item["module"]))
    return {
        "id": kind,
        "title": _title(kind, name),
        "contrary_to": _contrary(kind, name, upgrade_age),
        "risk": risk,
        "delta": round(risk - factual, 4),
        "drivers": [] if found is None else list(found.get("drivers") or []),
        "modules": modules[:6],
    }


def _drops(fact: dict, name: str, kind: str, upgrade_age: float | None) -> bool:
    predicate = fact.get("predicate")
    if kind == "withhold_upgrade" and predicate == "depends_on" and holds(fact, 0):
        if _pkg_name(str(fact.get("object") or "")) != name:
            return False
        opened = float(fact.get("from_age") or 0)
        return 0 < opened <= UPGRADE_WINDOW_DAYS
    if predicate == "attributed_to" and _pkg_name(str(fact.get("object") or "")) == name:
        if kind == "withhold_failure":
            return True
        if kind == "withhold_upgrade" and upgrade_age is not None:
            age = fact.get("at_age")
            return age is not None and float(age) < float(upgrade_age)
    return False


def _upgrade_age(facts: list[dict], name: str) -> float | None:
    ages = []
    for fact in facts:
        if fact.get("predicate") != "depends_on" or not holds(fact, 0):
            continue
        if _pkg_name(str(fact.get("object") or "")) != name:
            continue
        opened = float(fact.get("from_age") or 0)
        if 0 < opened <= UPGRADE_WINDOW_DAYS:
            ages.append(opened)
    return min(ages) if ages else None


def _title(kind: str, name: str) -> str:
    if kind == "withhold_upgrade":
        return f"The {name} upgrade did not happen"
    return f"The build failure was not attributed to {name}"


def _contrary(kind: str, name: str, upgrade_age: float | None) -> str:
    if kind == "withhold_upgrade":
        when = "the recorded day" if upgrade_age is None else f"{_age(upgrade_age)} days ago"
        return f"{name} was upgraded {when}. A build failure that came after that upgrade is absent from this world."
    return f"The upgrade and any native binding stay. Only the recorded build failure on {name} is removed."


def _summary(cases: list[dict]) -> str:
    if not cases:
        return "No recorded upgrade or build failure can be withheld."
    lead = cases[0]
    world = lead["worlds"][0]
    return (
        f"{world['title']} moves the {lead['package']} dependency index "
        f"from {lead['factual']['risk']:.2f} to {world['risk']:.2f}."
    )


def _pkg_name(label: str) -> str:
    if label.startswith("@") and label.count("@") >= 2:
        return label.rsplit("@", 1)[0]
    if "@" in label and not label.startswith("@"):
        return label.split("@", 1)[0]
    return label


def _age(value: float) -> str:
    number = float(value)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")
