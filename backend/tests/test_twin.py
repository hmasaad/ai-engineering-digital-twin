import copy
import json
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
CLIENT = "clients/web/checkout.ts"
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


def test_forecast_ranks_failures_and_widens_thin_history(twin):
    forecast = twin["forecast"]
    lead = forecast["failures"][0]
    retry = forecast["by_module"][RETRY]
    client = forecast["by_module"][CLIENT]
    assert lead["module"] == RETRY
    assert lead["mode"] == "bug"
    assert retry["low"] < retry["score"] < retry["high"] <= 1
    assert client["width"] > retry["width"]
    assert retry["confidence"] in {"medium", "high"}
    assert client["confidence"] == "low"
    assert forecast["band"]["bug_p90"] >= forecast["band"]["bug_p50"]
    assert "No outcomes" in forecast["confidence"]["note"]
    from app.forecast import build_forecast

    narrowed = build_forecast(twin["modules"], forecast["band"], 12)
    assert narrowed["by_module"][RETRY]["width"] < retry["width"]


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


def test_change_impact_follows_tests_packages_and_the_build(twin):
    impact = assess_change(twin, [RETRY])["impact"]
    kinds = {(path["kind"], path["object"]) for path in impact["paths"]}
    assert ("pull_request", RETRY) in {(path["kind"], path["object"]) for path in impact["paths"]}
    assert ("test", "tests/test_payments.py") in kinds
    assert ("failure", "psycopg") in kinds
    assert ("package", "psycopg@3.2.1") in kinds
    assert ("plugin", "libpq") in kinds
    assert "associated with psycopg" in impact["summary"]
    assert "requests" not in {path["object"] for path in impact["paths"]}

    driver = assess_change(twin, ["libs/common/db.py"])["impact"]
    package = next(path for path in driver["paths"] if path["kind"] == "package")
    assert package["object"] == "psycopg@3.2.1"
    assert package["via"] == "imported by this file"
    assert any(path["kind"] == "plugin" and path["object"] == "libpq" for path in driver["paths"])
    assert not any(path["kind"] == "failure" for path in driver["paths"])


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


def test_decision_keeps_tradeoffs_and_asks_before_acting_inside_the_band(raw):
    from app.decide import analyze

    report = run_counterfactual(raw, None, RETRY)
    wide = analyze(report, 0.25)
    front = {item["action"] for item in wide["pareto"]}
    assert "add_tests" in front
    assert "add_circuit_breaker" in front
    assert "freeze" not in front
    assert wide["status"] == "measure"
    assert wide["plan"]["now"]["action"] == "measure"
    assert "Record whether" in wide["plan"]["now"]["title"] or wide["information"]["changes_decision"]
    assert wide["plan"]["if_holds"]["action"] == "none"
    assert wide["plan"]["if_failure"]["action"] != "none"
    assert any(probe["changes_decision"] and probe["loss"] > 0 for probe in wide["probes"])
    assert wide["causal"]
    assert wide["causal"][0]["intervention"].startswith("do(")
    tight = analyze(report, 0.01)
    assert tight["status"] == "act"
    assert tight["plan"]["now"]["action"] != "measure"
    dominated = {item["action"] for item in wide["dominated"]}
    assert front.isdisjoint(dominated)


def test_policy_abandons_an_action_that_keeps_failing(raw):
    from app.optimize import apply_feedback, build_loop, empty_policy, reliability, apply_calibration

    first = build_loop(raw, None, RETRY, [], empty_policy())
    action = first["recommendation"]["action"]
    assert action != "none"
    assert "untested" in first["situation"]
    assert first["calibrated_score"] == first["raw_score"]
    policy = empty_policy()
    for _ in range(5):
        policy = apply_feedback(policy, first["situation"], action, happened=True, took=True)
    second = build_loop(raw, None, RETRY, [], policy)
    learned = next(item["learned"] for item in second["actions"] if item["action"] == action)
    assert learned < -0.5
    assert second["recommendation"]["action"] != action
    curve = reliability(
        [
            {"predicted": 0.9, "happened": False, "kind": "bug"},
            {"predicted": 0.85, "happened": False, "kind": "bug"},
            {"predicted": 0.8, "happened": False, "kind": "bug"},
        ]
    )
    high = next(band for band in curve["bins"] if band["name"] == "high")
    assert high["observed"] == 0
    assert high["gap"] > 0.5
    assert apply_calibration(0.86, curve) < 0.86
    assert apply_calibration(0.5, reliability([])) == 0.5


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


def test_engineering_graph_remembers_when_relations_held(twin):
    from app.temporal import holds

    facts = twin["dynamics"]["facts"]
    current = next(
        fact
        for fact in facts
        if fact["predicate"] == "depends_on" and fact["object"] == "psycopg@3.2.1"
    )
    prior = next(fact for fact in facts if fact["predicate"] == "depends_on" and fact["object"] == "psycopg@3.1.0")
    assert current["from_age"] == 21
    assert current["to_age"] is None
    assert prior["to_age"] == 21
    assert prior["from_age"] > 21
    assert holds(current, 10)
    assert not holds(current, 40)
    assert holds(prior, 40)
    assert not holds(prior, 10)

    pull = next(fact for fact in facts if fact["predicate"] == "modifies" and fact["subject"] == "pr:84")
    affected = next(fact for fact in facts if fact["predicate"] == "affects" and fact["subject"] == "pr:84")
    assert pull["object"] == RETRY
    assert pull["from_age"] == 19 and pull["to_age"] == 18
    assert affected["object"] == "tests/test_payments.py"
    assert holds(pull, 18)
    assert not holds(pull, 0)

    failure = next(fact for fact in facts if fact["predicate"] == "failed")
    link = next(fact for fact in facts if fact["predicate"] == "attributed_to")
    assert failure["subject"] == "build:ci-441"
    assert link["object"] == "psycopg"
    assert holds(failure, 0)
    assert not holds(failure, 20)
    plugin = next(fact for fact in facts if fact["predicate"] == "plugin" and fact["object"] == "libpq")
    assert holds(plugin, 0)
    assert not holds(plugin, 40)
    assert any(fact["predicate"] == "covers" and fact["object"] == STOCK for fact in facts)
    now = twin["dynamics"]["slices"][-1]
    earlier = next(item for item in twin["dynamics"]["slices"] if item["as_of"] == 20)
    assert "CI failed" in now["change"]
    assert "Upgrade psycopg" in earlier["change"]


def test_pubspec_plugin_is_a_package_relation(tmp_path):
    from app.scan import _pubspec_packages
    from app.temporal import knowledge_graph

    pubspec = tmp_path / "pubspec.yaml"
    pubspec.write_text(
        "\n".join(
            [
                "name: camera_plugin",
                "dependencies:",
                "  flutter:",
                "    sdk: flutter",
                "  camera: ^0.10.5",
                "flutter:",
                "  plugin:",
                "    platforms:",
                "      android:",
                "        package: com.salt.camera",
                "        pluginClass: CameraPlugin",
            ]
        ),
        encoding="utf-8",
    )
    specs = _pubspec_packages(pubspec, "pubspec.yaml")
    facts = knowledge_graph({"modules": [], "edges": [], "events": [], "packages": specs})
    assert any(fact["predicate"] == "depends_on" and fact["object"] == "camera@^0.10.5" for fact in facts)
    assert any(
        fact["predicate"] == "plugin" and fact["subject"] == "camera_plugin" and fact["object"] == "com.salt.camera"
        for fact in facts
    )


def test_sync_notices_a_new_package_pin(raw):
    from app.sync import compare

    observed = json.loads(json.dumps(raw))
    observed["packages"] = list(observed["packages"]) + [
        {"name": "http", "version": "1.2.0", "manifest": "pubspec.yaml", "plugins": ["android/http"]}
    ]
    report = compare(raw, observed)
    assert any(event["kind"] == "package_added" and "http@1.2.0" in event["summary"] for event in report["events"])
    bare = {key: value for key, value in raw.items() if key != "packages"}
    missing = compare(bare, raw)
    assert any(event["kind"] == "packages_observed" for event in missing["events"])


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


def _step(agent, severity, modules, effort="S", title=None):
    return {
        "agent": agent,
        "severity": severity,
        "title": title or f"{agent} step",
        "modules": modules,
        "effort": effort,
        "remediation": "Do the work in the repository.",
        "expected_effect": "Pressure falls.",
    }


def test_run_records_a_safe_measurement_and_holds_the_rest():
    from app.autonomy import build_mission

    mission = build_mission(
        [
            _step("Quality", "medium", ["services/payments/retry.py"], title="Tests"),
            _step("Security", "critical", ["services/payments/retry.py"], title="Secret"),
            _step("Architect", "high", ["services/inventory/stock.py", "services/checkout/checkout.py"], effort="L"),
        ]
    )
    quality, security, architect = mission["tasks"]
    assert mission["writes_repository"] is False
    assert quality["status"] == "done"
    assert quality["approver"] == "twin"
    assert quality["repository"] == "unchanged"
    assert "not edited" in quality["result"]
    assert security["status"] == "awaiting"
    assert security["action"] == "hold"
    assert security["waits_on"] == "t1"
    assert "follows Quality" in security["coordination"]
    assert architect["status"] == "awaiting"
    assert "Blast radius is 2 modules" in architect["gate"]
    assert "Large effort" in architect["gate"]


def test_rejection_replans_the_next_agent_as_a_measurement():
    from app.autonomy import build_mission, decide

    mission = build_mission(
        [
            _step("Security", "critical", [RETRY], title="Secret"),
            _step("Quality", "high", [RETRY], title="Tests"),
        ]
    )
    assert mission["tasks"][1]["status"] == "waiting"
    decide(mission, "t1", False)
    follower = mission["tasks"][1]
    assert mission["tasks"][0]["status"] == "rejected"
    assert follower["status"] == "done"
    assert follower["action"] == "measure"
    assert follower["recovered"] is True
    assert follower["approver"] == "twin"
    assert mission["status"] == "recovered"
    assert mission["writes_repository"] is False


def test_a_failed_step_narrows_the_follower():
    from app.autonomy import build_mission, fail

    mission = build_mission(
        [
            _step("Quality", "medium", [RETRY]),
            _step("Delivery", "high", [RETRY]),
        ]
    )
    assert mission["tasks"][1]["status"] == "awaiting"
    assert "follows Quality" in mission["tasks"][1]["coordination"]
    fail(mission, "t1")
    assert mission["tasks"][0]["status"] == "failed"
    assert mission["tasks"][1]["status"] == "done"
    assert mission["tasks"][1]["action"] == "measure"
    assert "Recovered after Quality" in mission["tasks"][1]["gate"]


def test_learning_is_once_and_only_after_the_step_is_done():
    from app.autonomy import build_mission, mark_learned

    mission = build_mission([_step("Quality", "medium", [RETRY])])
    mark_learned(mission, "t1", True, "Confirmed bug on retry.py")
    assert mission["tasks"][0]["learned"] is True
    assert mission["tasks"][0]["outcome"] is True
    with pytest.raises(KeyError):
        mark_learned(mission, "t1", False, "again")


def test_northwind_run_keeps_security_with_a_person(twin):
    from app.autonomy import build_mission

    mission = build_mission(twin["plan"])
    first = mission["tasks"][0]
    assert first["agent"] == "Security"
    assert first["status"] == "awaiting"
    assert first["action"] == "hold"
    assert all(task["repository"] == "unchanged" for task in mission["tasks"])
    assert mission["writes_repository"] is False


def test_graph_edges_are_resolved_imports_and_simulation_is_not_evidence(raw, twin):
    from app.fidelity import PRINCIPLE, assess

    known = {module["path"] for module in raw["modules"]}
    assert raw["edges"]
    assert all(edge["source"] in known and edge["target"] in known for edge in raw["edges"])
    report = assess(twin, raw, [])
    assert report["graph"]["resolved"] == report["graph"]["edges"]
    assert report["graph"]["dangling"] == 0
    assert "dynamic imports" in report["graph"]["note"]
    assert "does not install" in report["graph"]["note"]
    assert report["graph"]["coverage"] is not None
    assert report["principle"] == PRINCIPLE
    assert "not evidence" in report["principle"]
    assert report["channels"]["production"]["n"] >= 1
    assert any(check["source"] == "history" and check.get("service") == "payments" for check in report["checks"])
    payments = next(row for row in report["improvement"]["rows"] if row["module"] == RETRY)
    assert payments["status"] == "unproven"
    assert "No recommended action" in payments["detail"]
    assert "correlation" in report["causation"]["association"]
    assert "not a measured cause" in report["causation"]["intervention"]
    assert report["verdict"] != "proven"


def test_a_band_across_the_midpoint_does_not_confirm_the_score():
    from app.fidelity import assess, judge

    assert judge(0.82, 0.70, 0.94, False) == "agreed"
    assert judge(0.82, 0.70, 0.94, True) == "false_alarm"
    assert judge(0.20, 0.05, 0.35, False) == "missed"
    assert judge(0.55, 0.30, 0.80, False) == "uncertain"
    assert judge(0.55, 0.30, 0.80, True) == "uncertain"
    checks = [
        _check(RETRY, "2026-01-01T00:00:00+00:00", passed=False),
        _check(RETRY, "2026-01-02T00:00:00+00:00", passed=False, action="add_tests"),
        _check(RETRY, "2026-01-03T00:00:00+00:00", passed=True),
        _check(STOCK, "2026-01-01T00:00:00+00:00", passed=False),
        _check(STOCK, "2026-01-03T00:00:00+00:00", passed=False),
    ]
    report = assess({"risks": [], "outcomes": [], "weight_history": [], "forecast": {}, "modules": []}, {"modules": [], "edges": [], "dependencies": [], "events": []}, checks)
    row = next(item for item in report["improvement"]["rows"] if item["module"] == RETRY)
    assert row["status"] == "consistent"
    assert "not proof" in row["detail"]
    assert "stock.py" in row["detail"]
    assert "caused" not in row["detail"].split("not proof")[0]
    late = assess(
        {"risks": [], "outcomes": [], "weight_history": [], "forecast": {}, "modules": []},
        {"modules": [], "edges": [], "dependencies": [], "events": []},
        [_check(RETRY, "2026-01-01T00:00:00+00:00", passed=False), _check(RETRY, "2026-01-02T00:00:00+00:00", passed=True, action="add_tests")],
    )
    assert late["improvement"]["rows"][0]["status"] == "unproven"


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


def test_api_run_approves_without_editing_and_then_learns(tmp_path, monkeypatch):
    monkeypatch.setenv("TWIN_DB", str(tmp_path / "run.sqlite"))
    os.environ["TWIN_DB"] = str(tmp_path / "run.sqlite")
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        started = client.post("/api/run")
        assert started.status_code == 200, started.text
        mission = started.json()["mission"]
        assert mission["writes_repository"] is False
        security = next(task for task in mission["tasks"] if task["agent"] == "Security")
        assert security["status"] == "awaiting"
        approved = client.post("/api/run/approve", json={"task": security["id"], "approve": True})
        assert approved.status_code == 200, approved.text
        done = next(task for task in approved.json()["mission"]["tasks"] if task["id"] == security["id"])
        assert done["status"] == "done"
        assert done["approver"] == "human"
        assert done["repository"] == "unchanged"
        learned = client.post("/api/run/learn", json={"task": security["id"], "happened": True})
        assert learned.status_code == 200, learned.text
        body = learned.json()
        assert body["learning"]["deltas"]
        assert body["twin"]["calibration"]["n"] == 1
        updated = next(task for task in body["mission"]["tasks"] if task["id"] == security["id"])
        assert updated["learned"] is True
        again = client.post("/api/run/learn", json={"task": security["id"], "happened": False})
        assert again.status_code == 400


def _check(module, created, passed, action=""):
    return {
        "module": module,
        "channel": "production",
        "kind": "incident",
        "passed": passed,
        "predicted": 0.8,
        "low": 0.7,
        "high": 0.9,
        "action": action,
        "note": "",
        "verdict": "agreed",
        "source": "recorded",
        "created_at": created,
    }


def test_agreed_checks_narrow_only_the_matching_mode(twin):
    from app.forecast import apply_agreed

    before = twin["forecast"]["by_module"][RETRY]["width"]
    narrowed = apply_agreed(twin["forecast"], [{"module": RETRY, "kind": "bug", "verdict": "agreed"}])
    assert narrowed["by_module"][RETRY]["width"] < before
    assert narrowed["by_module"][RETRY]["agreed_checks"] == 1
    assert narrowed["by_module"][CLIENT]["width"] == twin["forecast"]["by_module"][CLIENT]["width"]
    again = apply_agreed(narrowed, [{"module": RETRY, "kind": "bug", "verdict": "agreed"}])
    assert again["by_module"][RETRY]["width"] == narrowed["by_module"][RETRY]["width"]
    untouched = apply_agreed(twin["forecast"], [{"module": RETRY, "kind": "incident", "verdict": "agreed"}])
    assert untouched["by_module"][RETRY]["width"] == before


def test_api_check_learns_and_does_not_treat_the_simulation_as_proof(tmp_path, monkeypatch):
    monkeypatch.setenv("TWIN_DB", str(tmp_path / "fidelity.sqlite"))
    os.environ["TWIN_DB"] = str(tmp_path / "fidelity.sqlite")
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        before = client.get("/api/twin").json()["calibration"]["n"]
        recorded = client.post(
            "/api/fidelity/checks",
            json={"module": RETRY, "channel": "build", "passed": False, "note": "isolated build"},
        )
        assert recorded.status_code == 200, recorded.text
        body = recorded.json()
        assert body["check"]["verdict"] == "recorded"
        assert body["check"]["kind"] == "build"
        assert body["learning"]["skipped"] is True
        assert body["learning"]["deltas"] == []
        assert body["twin"]["calibration"]["n"] == before
        assert "not evidence" in body["fidelity"]["principle"]
        assert body["fidelity"]["channels"]["build"]["n"] == 1
        assert body["fidelity"]["channels"]["build"]["recorded"] == 1
        assert any(check["source"] == "history" for check in body["fidelity"]["checks"])
        assert body["fidelity"]["verdict"] != "proven"
        fetched = client.get("/api/fidelity")
        assert fetched.status_code == 200
        assert fetched.json()["channels"]["production"]["n"] >= 1
