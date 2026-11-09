import copy
import os

import pytest

from app.graph import find_cycles
from app.impact import assess_change
from app.report import build_twin
from app.scan import scan_repository
from app.score import learn, merge_models
from app.settings import EXAMPLE
from app.simulate import run_counterfactual, run_scenario

RETRY = "services/payments/retry.py"
CHARGE = "services/payments/charge.py"
CHECKOUT = "services/checkout/checkout.py"
STOCK = "services/inventory/stock.py"
GATEWAY = "services/gateway/main.py"
PRICING = "services/checkout/pricing.py"


@pytest.fixture(scope="module")
def raw():
    return scan_repository(EXAMPLE)


@pytest.fixture(scope="module")
def twin(raw):
    return build_twin(raw)


def test_two_node_cycle_is_canonical():
    cycles = find_cycles(
        [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}, {"source": "a", "target": "a"}]
    )
    assert cycles == [["a", "b"]]


def test_history_and_structure(raw):
    kinds = {event["type"] for event in raw["events"]}
    assert {"commit", "incident", "deploy", "review"} <= kinds
    assert len(raw["events"]) >= 20
    assert "psycopg" in raw["dependencies"]
    assert "requests" in raw["dependencies"]
    assert raw["description"].startswith("Northwind Exchange")
    db = next(module for module in raw["modules"] if module["path"] == "libs/common/db.py")
    kinds = {item["kind"] for item in db["evidence"]}
    assert "sql-injection" in kinds
    assert "select-star" in kinds
    docker = next(module for module in raw["modules"] if module["path"] == "deploy/Dockerfile")
    assert any(item["kind"] == "container-root" for item in docker["evidence"])


def test_coverage_and_cycle(twin):
    by_path = {module["path"]: module for module in twin["modules"]}
    assert by_path[PRICING]["test_gap"] == 0
    assert by_path[STOCK]["test_gap"] == 0
    assert by_path[RETRY]["test_gap"] == 1
    pairs = {(edge["source"], edge["target"]) for edge in twin["edges"]}
    assert (CHARGE, RETRY) in pairs
    assert (CHECKOUT, STOCK) in pairs
    assert (STOCK, CHECKOUT) in pairs
    assert any("cycle" in finding["title"].lower() for finding in twin["findings"])


def test_retry_is_the_top_bug_risk(twin):
    bugs = [risk for risk in twin["risks"] if risk["kind"] == "bug"]
    retry = next(module for module in twin["modules"] if module["path"] == RETRY)
    ranking = [(risk["module"], risk["score"], risk["drivers"]) for risk in bugs[:8]]
    assert bugs[0]["module"] == RETRY, {"ranking": ranking, "features": retry["features"], "risks": retry["risks"]}
    assert retry["features"]["test_pressure"] > 0.4
    assert "retry.py" in twin["narrative"]
    assert 20 <= twin["health"] <= 90


def test_security_leads_the_plan(twin):
    assert any(finding["agent"] == "Security" and finding["severity"] == "critical" for finding in twin["findings"])
    assert twin["plan"]
    assert twin["plan"][0]["agent"] == "Security"
    assert [step["step"] for step in twin["plan"]] == list(range(1, len(twin["plan"]) + 1))
    assert {step["effort"] for step in twin["plan"]} <= {"S", "M", "L"}


def test_blast_radius_reaches_the_gateway(twin):
    impact = assess_change(twin, [RETRY], title="Change retry backoff")
    reached = {item["module"] for item in impact["blast"]}
    assert {RETRY, CHARGE, CHECKOUT, GATEWAY} <= reached
    assert impact["risk"] > 0.2
    assert RETRY in impact["missing_tests"]
    assert impact["objections"]
    with pytest.raises(KeyError):
        assess_change(twin, ["missing.py"])


def test_scenarios_move_the_indexes(raw):
    tests = run_scenario(raw, None, "tests", [{"action": "add_tests", "module": RETRY}])
    assert tests["simulated"]["expected_bug_modules"] < tests["baseline"]["expected_bug_modules"] - 0.05
    bug = next(row for row in tests["movers"] if row["module"] == RETRY and row["metric"] == "bug")
    assert bug["after"] < bug["before"]

    frozen = run_scenario(raw, None, "freeze", [{"action": "freeze", "module": RETRY}])
    frozen_bug = next(row for row in frozen["movers"] if row["module"] == RETRY and row["metric"] == "bug")
    frozen_delay = next(row for row in frozen["movers"] if row["module"] == RETRY and row["metric"] == "delay")
    assert frozen_bug["after"] < frozen_bug["before"]
    assert frozen_delay["after"] > frozen_delay["before"]

    extracted = run_scenario(raw, None, "extract", [{"action": "extract_module", "module": STOCK}])
    assert extracted["simulated"]["cycles"] < extracted["baseline"]["cycles"]

    breaker = run_scenario(raw, None, "breaker", [{"action": "add_circuit_breaker", "module": RETRY}])
    assert breaker["simulated"]["expected_incident_services"] < breaker["baseline"]["expected_incident_services"]


def test_counterfactual_compares_alternatives_with_what_happened(raw):
    report = run_counterfactual(raw, None, RETRY)
    assert "no linked tests" in report["factual"]["summary"]
    regrets = [item["regret"] for item in report["alternatives"]]
    assert regrets == sorted(regrets)
    assert report["preferred"] == report["alternatives"][0]["action"]
    assert sum(1 for item in report["alternatives"] if item["preferred"]) == 1
    tests = next(item for item in report["alternatives"] if item["action"] == "add_tests")
    frozen = next(item for item in report["alternatives"] if item["action"] == "freeze")
    assert "No characterization tests" in tests["contrary_to"]
    assert tests["deltas"]["expected_bug_modules"] < 0
    assert tests["local"]["bug"] < report["factual"]["bug"]
    assert frozen["local"]["bug"] < report["factual"]["bug"]
    assert frozen["local"]["delay"] > report["factual"]["delay"]

    stock = run_counterfactual(raw, None, STOCK)
    extracted = next(item for item in stock["alternatives"] if item["action"] == "extract_module")
    assert stock["factual"]["on_cycle"] is True
    assert "import cycle" in extracted["contrary_to"]
    assert extracted["deltas"]["cycles"] < 0
    with pytest.raises(KeyError):
        run_counterfactual(raw, None, "missing.py")


def test_learning_nudges_the_active_feature(twin):
    retry = next(module for module in twin["modules"] if module["path"] == RETRY)
    models = merge_models(None)
    before = models["bug"]["weights"]["test_pressure"]
    updated, _bias, predicted, error = learn(
        models["bug"]["weights"],
        models["bug"]["bias"],
        retry["features"],
        1,
    )
    assert error > 0
    assert predicted < 1
    assert updated["test_pressure"] > before


def test_a_new_incident_raises_service_risk(raw):
    before = build_twin(raw)
    raw2 = copy.deepcopy(raw)
    raw2["events"].append(
        {
            "ts": raw["scanned_at"],
            "age_days": 0,
            "type": "incident",
            "service": "notify",
            "files": [],
            "summary": "mail outage",
            "data": {
                "author": None,
                "severity": "sev1",
                "result": None,
                "hours": None,
                "file_stats": {},
            },
        }
    )
    after = build_twin(raw2)

    def incident(view):
        modules = [module for module in view["modules"] if module["service"] == "notify" and module["role"] == "code"]
        return max(module["risks"]["incident"] for module in modules)

    assert incident(after) > incident(before)


def test_time_replays_the_system_and_keeps_intervals(twin):
    dynamics = twin["dynamics"]
    slices = dynamics["slices"]
    assert slices[0]["as_of"] > slices[-1]["as_of"] == 0
    counts = [item["modules"] for item in slices]
    assert counts == sorted(counts)
    past = next(item for item in slices if item["as_of"] == 80)
    mid = next(item for item in slices if item["as_of"] == 40)
    now = slices[-1]
    assert RETRY not in past["module_paths"]
    assert RETRY in mid["module_paths"]
    assert "clients/web/checkout.ts" in now["module_paths"]
    assert "clients/web/checkout.ts" not in mid["module_paths"]
    assert mid["incident"] > past["incident"]

    def pairs(item):
        return {(edge["source"], edge["target"]) for edge in item["graph"]["imports"]}

    assert (CHARGE, RETRY) not in pairs(past)
    assert (CHARGE, RETRY) in pairs(mid)
    edge = next(
        fact
        for fact in dynamics["facts"]
        if fact["predicate"] == "imports" and fact["subject"] == CHARGE and fact["object"] == RETRY
    )
    assert edge["kind"] == "interval"
    assert edge["to_age"] is None
    assert edge["from_age"] < 80
    assert any(fact["predicate"] == "incident_on" for fact in dynamics["facts"])
    assert any(RETRY in item["change"] or "retry.py" in item["change"] for item in slices)


def test_sync_is_quiet_when_nothing_moved(raw):
    from app.sync import compare

    again = scan_repository(EXAMPLE)
    report = compare(raw, again)
    assert report["status"] == "aligned"
    assert report["events"] == []


def test_sync_emits_events_for_structure_and_history(raw):
    from app.sync import compare

    observed = copy.deepcopy(raw)
    observed["modules"] = [module for module in observed["modules"] if module["path"] != RETRY]
    observed["edges"] = [edge for edge in observed["edges"] if RETRY not in (edge["source"], edge["target"])]
    report = compare(raw, observed)
    kinds = {event["kind"] for event in report["events"]}
    assert report["status"] == "drifted"
    assert "module_removed" in kinds
    assert any(event["kind"] == "import_removed" and event["subject"] == CHARGE and event["object"] == RETRY for event in report["events"])

    grown = copy.deepcopy(raw)
    retry = next(module for module in grown["modules"] if module["path"] == RETRY)
    retry["loc"] += 12
    changed = compare(raw, grown)
    assert any(event["kind"] == "module_changed" and event["subject"] == RETRY and "loc" in event["summary"] for event in changed["events"])

    recorded = copy.deepcopy(raw)
    recorded["events"] = list(recorded["events"]) + [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "age_days": 1,
            "clock": "age",
            "type": "incident",
            "service": "notify",
            "files": [],
            "summary": "Pager missed a page",
            "data": {"author": None, "severity": "sev3", "result": None, "hours": None, "file_stats": {}},
        }
    ]
    added = compare(raw, recorded)
    assert any(event["kind"] == "history_added" and "Pager missed a page" in event["summary"] for event in added["events"])


def test_sync_applies_repository_drift_and_keeps_virtual_events(tmp_path, monkeypatch):
    monkeypatch.setenv("TWIN_DB", str(tmp_path / "twin.sqlite"))
    os.environ["TWIN_DB"] = str(tmp_path / "twin.sqlite")
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.store import inspect_sync, synchronize

    observed = scan_repository(EXAMPLE)
    observed["modules"] = [module for module in observed["modules"] if module["path"] != RETRY]
    observed["edges"] = [edge for edge in observed["edges"] if RETRY not in (edge["source"], edge["target"])]

    with TestClient(create_app()) as client:
        quiet = client.get("/api/sync")
        assert quiet.status_code == 200
        assert quiet.json()["status"] == "aligned"
        noted = client.post(
            "/api/events",
            json={"type": "incident", "service": "notify", "severity": "sev3", "summary": "Pager missed a page"},
        )
        assert noted.status_code == 200
        pending = inspect_sync()
        assert pending["status"] == "aligned"
        assert any("Pager missed a page" in event["summary"] for event in pending["virtual_only"])

        def fake_scan(root, now=None):
            return observed

        monkeypatch.setattr("app.store.scan_repository", fake_scan)
        still = client.get("/api/twin").json()
        assert any(module["path"] == RETRY for module in still["modules"])
        drifted = inspect_sync()
        assert drifted["status"] == "drifted"
        assert drifted["applied"] is False
        applied = synchronize()
        assert applied["sync"]["status"] == "drifted"
        assert any(fact["predicate"] == "reconciled" and fact["subject"] == "module_removed" for fact in applied["twin"]["dynamics"]["facts"])
        assert all(module["path"] != RETRY for module in applied["twin"]["modules"])
        assert any("Pager missed a page" in event["summary"] for event in applied["twin"]["timeline"])


def test_build_is_deterministic(raw):
    assert build_twin(raw)["health"] == build_twin(raw)["health"]


def test_api_learns_from_an_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("TWIN_DB", str(tmp_path / "twin.sqlite"))
    os.environ["TWIN_DB"] = str(tmp_path / "twin.sqlite")
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        health = client.get("/api/healthz")
        assert health.status_code == 200
        body = client.get("/api/twin")
        assert body.status_code == 200
        twin = body.json()
        assert twin["project"]["name"] == "northwind"
        recorded = client.post("/api/outcomes", json={"module": RETRY, "kind": "bug", "happened": True})
        assert recorded.status_code == 200, recorded.text
        payload = recorded.json()
        assert payload["learning"]["deltas"]
        assert payload["twin"]["calibration"]["n"] == 1
        assert payload["twin"]["calibration"]["brier"] is not None
