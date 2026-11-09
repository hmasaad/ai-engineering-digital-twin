"""SQLite persistence for scans, beliefs, outcomes, and recorded events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.autonomy import build_mission, decide, fail, idle_mission, mark_learned, prepare_learning
from app.fidelity import assess, history_checks, prepare_check, updates_model
from app.forecast import apply_agreed
from app.optimize import apply_feedback, build_loop, empty_policy, situation
from app.report import build_twin
from app.scan import event_from_payload, scan_repository
from app.score import LABELS, learn, merge_models
from app.settings import database_path
from app.sync import compare, unmatched
from app.temporal import reconciled_facts

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  path TEXT PRIMARY KEY,
  raw_json TEXT NOT NULL,
  view_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS models (
  path TEXT PRIMARY KEY,
  models_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  module TEXT NOT NULL,
  kind TEXT NOT NULL,
  happened INTEGER NOT NULL,
  predicted REAL NOT NULL,
  features_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weight_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  note TEXT NOT NULL,
  deltas_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  event_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy (
  path TEXT PRIMARY KEY,
  policy_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  module TEXT NOT NULL,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL,
  passed INTEGER NOT NULL,
  predicted REAL NOT NULL,
  low REAL NOT NULL,
  high REAL NOT NULL,
  action TEXT NOT NULL,
  note TEXT NOT NULL,
  verdict TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission (
  path TEXT PRIMARY KEY,
  mission_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  status TEXT NOT NULL,
  applied INTEGER NOT NULL,
  events_json TEXT NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def ensure_ready(example: Path) -> None:
    init_db()
    if active_path() is None and example.is_dir():
        ingest(example)


def ingest(root: Path) -> dict:
    observed = scan_repository(root)
    init_db()
    path = str(root.resolve())
    with _connect() as conn:
        previous = _stored_raw(conn, path)
        report = compare(previous, observed)
        report["virtual_only"] = [] if previous is None else unmatched(_user_events(conn, path), observed.get("events") or [])
        report["applied"] = True
        conn.execute(
            """
            INSERT INTO projects (path, raw_json, view_json, updated_at)
            VALUES (?, ?, '{}', ?)
            ON CONFLICT(path) DO UPDATE SET raw_json = excluded.raw_json, updated_at = excluded.updated_at
            """,
            (path, json.dumps(observed), _now()),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('active_path', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (path,),
        )
        _write_sync(conn, path, report)
    return recompute(path)


def inspect_sync() -> dict:
    """Read the repository and report drift. Does not change the virtual state."""
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    observed = scan_repository(Path(path))
    with _connect() as conn:
        previous = _stored_raw(conn, path)
        report = compare(previous, observed)
        report["virtual_only"] = unmatched(_user_events(conn, path), observed.get("events") or [])
    report["applied"] = False
    return report


def synchronize() -> dict:
    """Apply repository drift to the virtual twin and record the events."""
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    twin = ingest(Path(path))
    return {"sync": twin.get("sync"), "twin": twin}


def recompute(path: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT raw_json FROM projects WHERE path = ?", (path,)).fetchone()
        if row is None:
            raise LookupError(f"No scan for {path}")
        raw = json.loads(row["raw_json"])
        raw["events"] = list(raw["events"]) + _user_events(conn, path)
        models = _models(conn, path)
        outcomes = _outcomes(conn, path)
        history = _history(conn, path)
        view = build_twin(raw, models, outcomes, history)
        stored_checks = _checks(conn, path)
        view["forecast"] = apply_agreed(view["forecast"], stored_checks + history_checks(view, raw))
        code = [module for module in view["modules"] if module["role"] == "code"]
        if code:
            lead = max(code, key=lambda module: module["risks"]["bug"])
            view["loop"] = build_loop(raw, models, lead["path"], outcomes, _policy(conn, path))
        else:
            view["loop"] = None
        sync = _latest_sync(conn, path)
        view["sync"] = sync
        if sync is not None:
            drifted = _latest_drift(conn, path)
            if drifted is not None:
                view["dynamics"]["facts"].extend(reconciled_facts(drifted["events"], drifted["age_days"]))
        conn.execute(
            "UPDATE projects SET view_json = ?, updated_at = ? WHERE path = ?",
            (json.dumps(view), _now(), path),
        )
        conn.execute(
            """
            INSERT INTO models (path, models_json) VALUES (?, ?)
            ON CONFLICT(path) DO UPDATE SET models_json = excluded.models_json
            """,
            (path, json.dumps(view["models"])),
        )
    return view


def current_view() -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    with _connect() as conn:
        row = conn.execute("SELECT view_json FROM projects WHERE path = ?", (path,)).fetchone()
    if row is None or row["view_json"] in (None, "", "{}"):
        return recompute(path)
    return json.loads(row["view_json"])


def active_raw() -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    with _connect() as conn:
        row = conn.execute("SELECT raw_json FROM projects WHERE path = ?", (path,)).fetchone()
        if row is None:
            raise LookupError("No project scanned")
        raw = json.loads(row["raw_json"])
        raw["events"] = list(raw["events"]) + _user_events(conn, path)
        return raw


def active_path() -> str | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'active_path'").fetchone()
    return None if row is None else row["value"]


def record_outcome(module: str, kind: str, happened: bool) -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    view = current_view()
    target = next((item for item in view["modules"] if item["path"] == module), None)
    if target is None or target["role"] != "code":
        raise KeyError(f"Unknown module: {module}")
    if kind not in view["models"]:
        raise KeyError(f"Unknown model: {kind}")
    features = target["features"]
    model = view["models"][kind]
    updated, bias, predicted, error = learn(model["weights"], model["bias"], features, 1.0 if happened else 0.0)
    deltas = _deltas(model["weights"], updated, model["bias"], bias)
    note = f"{'Confirmed' if happened else 'Rejected'} {kind} on {module}"
    created = _now()
    with _connect() as conn:
        models = json.loads(conn.execute("SELECT models_json FROM models WHERE path = ?", (path,)).fetchone()["models_json"])
        models[kind]["weights"] = updated
        models[kind]["bias"] = bias
        conn.execute("UPDATE models SET models_json = ? WHERE path = ?", (json.dumps(models), path))
        conn.execute(
            """
            INSERT INTO weight_history (path, kind, note, deltas_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (path, kind, note, json.dumps(deltas), created),
        )
        conn.execute(
            """
            INSERT INTO outcomes (path, module, kind, happened, predicted, features_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (path, module, kind, 1 if happened else 0, predicted, json.dumps(features), created),
        )
    twin = recompute(path)
    return {
        "learning": {
            "kind": kind,
            "module": module,
            "happened": happened,
            "predicted": round(predicted, 4),
            "error": round(error, 4),
            "deltas": deltas,
            "note": note,
        },
        "twin": twin,
    }


def record_loop(module: str, action: str, happened: bool, kind: str = "bug") -> dict:
    """Record the outcome, then take one policy step for the action that was taken."""
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    view = current_view()
    target = next((item for item in view["modules"] if item["path"] == module and item["role"] == "code"), None)
    if target is None:
        raise KeyError(f"Unknown module: {module}")
    state = situation(target)
    result = record_outcome(module, kind, happened)
    with _connect() as conn:
        policy = apply_feedback(_policy(conn, path), state, action, happened, action != "none")
        _save_policy(conn, path, policy)
    twin = recompute(path)
    result["twin"] = twin
    result["loop"] = twin.get("loop")
    result["learning"]["situation"] = state
    result["learning"]["action"] = action
    return result


def fidelity_report() -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    view = current_view()
    raw = active_raw()
    with _connect() as conn:
        checks = _checks(conn, path)
    return assess(view, raw, checks)


def record_check(module: str, channel: str, passed: bool, action: str = "", note: str = "") -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    prepared = prepare_check(current_view(), module, channel, passed, action, note)
    created = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO checks (
              path, module, channel, kind, passed, predicted, low, high, action, note, verdict, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path,
                prepared["module"],
                prepared["channel"],
                prepared["kind"],
                1 if prepared["passed"] else 0,
                prepared["predicted"],
                prepared["low"],
                prepared["high"],
                prepared["action"],
                prepared["note"],
                prepared["verdict"],
                created,
            ),
        )
    prepared["created_at"] = created
    if updates_model(prepared):
        result = record_outcome(prepared["module"], prepared["kind"], not prepared["passed"])
        result["learning"]["skipped"] = False
    else:
        reason = (
            "The band crosses 0.50, so this check was kept and the model was not updated."
            if prepared["verdict"] == "uncertain"
            else "A build or test result is kept as evidence. It does not update the bug index."
        )
        result = {"learning": {"skipped": True, "deltas": [], "note": reason}, "twin": current_view()}
    result["check"] = prepared
    result["fidelity"] = fidelity_report()
    return result


def current_mission() -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    with _connect() as conn:
        return _load_mission(conn, path)


def start_run() -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    view = current_view()
    mission = build_mission(view.get("plan") or [])
    with _connect() as conn:
        _save_mission(conn, path, mission)
    return {"mission": mission, "twin": view}


def decide_run(task_id: str, approve: bool) -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    with _connect() as conn:
        mission = _load_mission(conn, path)
        decide(mission, task_id, approve)
        _save_mission(conn, path, mission)
    return {"mission": mission, "twin": current_view()}


def fail_run(task_id: str) -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    with _connect() as conn:
        mission = _load_mission(conn, path)
        fail(mission, task_id)
        _save_mission(conn, path, mission)
    return {"mission": mission, "twin": current_view()}


def learn_run(task_id: str, happened: bool) -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    with _connect() as conn:
        mission = _load_mission(conn, path)
        module, kind = prepare_learning(mission, task_id)
    result = record_outcome(module, kind, happened)
    with _connect() as conn:
        mission = _load_mission(conn, path)
        mark_learned(mission, task_id, happened, result["learning"]["note"])
        _save_mission(conn, path, mission)
    result["mission"] = mission
    result["twin"] = current_view()
    return result


def record_event(payload: dict) -> dict:
    path = active_path()
    if path is None:
        raise LookupError("No project scanned")
    event = event_from_payload(payload)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_events (path, event_json, created_at) VALUES (?, ?, ?)",
            (path, json.dumps(event), _now()),
        )
    return recompute(path)


def _deltas(before_weights: dict, after_weights: dict, before_bias: float, after_bias: float) -> list[dict]:
    rows = []
    pairs = [(key, before_weights[key], after_weights[key]) for key in before_weights]
    pairs.append(("bias", before_bias, after_bias))
    for key, before, after in pairs:
        old = round(float(before), 4)
        new = round(float(after), 4)
        if old == new:
            continue
        rows.append({"feature": key, "label": LABELS.get(key, key), "before": old, "after": new})
    rows.sort(key=lambda row: abs(row["after"] - row["before"]), reverse=True)
    return rows


def _models(conn: sqlite3.Connection, path: str) -> dict:
    row = conn.execute("SELECT models_json FROM models WHERE path = ?", (path,)).fetchone()
    if row is None:
        return merge_models(None)
    return merge_models(json.loads(row["models_json"]))


def _outcomes(conn: sqlite3.Connection, path: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT module, kind, happened, predicted, created_at
        FROM outcomes WHERE path = ? ORDER BY id DESC LIMIT 20
        """,
        (path,),
    ).fetchall()
    return [
        {
            "module": row["module"],
            "kind": row["kind"],
            "happened": bool(row["happened"]),
            "predicted": row["predicted"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _history(conn: sqlite3.Connection, path: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT kind, note, deltas_json, created_at
        FROM weight_history WHERE path = ? ORDER BY id DESC LIMIT 12
        """,
        (path,),
    ).fetchall()
    return [
        {
            "kind": row["kind"],
            "note": row["note"],
            "created_at": row["created_at"],
            "deltas": json.loads(row["deltas_json"]),
        }
        for row in rows
    ]


def _policy(conn: sqlite3.Connection, path: str) -> dict:
    row = conn.execute("SELECT policy_json FROM policy WHERE path = ?", (path,)).fetchone()
    if row is None:
        return empty_policy()
    saved = json.loads(row["policy_json"])
    base = empty_policy()
    base.update(saved)
    return base


def _checks(conn: sqlite3.Connection, path: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT module, channel, kind, passed, predicted, low, high, action, note, verdict, created_at
        FROM checks WHERE path = ? ORDER BY id ASC LIMIT 40
        """,
        (path,),
    ).fetchall()
    return [
        {
            "module": row["module"],
            "channel": row["channel"],
            "kind": row["kind"],
            "passed": bool(row["passed"]),
            "predicted": row["predicted"],
            "low": row["low"],
            "high": row["high"],
            "action": row["action"],
            "note": row["note"],
            "verdict": row["verdict"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _load_mission(conn: sqlite3.Connection, path: str) -> dict:
    row = conn.execute("SELECT mission_json FROM mission WHERE path = ?", (path,)).fetchone()
    if row is None:
        return idle_mission()
    return json.loads(row["mission_json"])


def _save_mission(conn: sqlite3.Connection, path: str, mission: dict) -> None:
    conn.execute(
        """
        INSERT INTO mission (path, mission_json) VALUES (?, ?)
        ON CONFLICT(path) DO UPDATE SET mission_json = excluded.mission_json
        """,
        (path, json.dumps(mission)),
    )


def _save_policy(conn: sqlite3.Connection, path: str, policy: dict) -> None:
    conn.execute(
        """
        INSERT INTO policy (path, policy_json) VALUES (?, ?)
        ON CONFLICT(path) DO UPDATE SET policy_json = excluded.policy_json
        """,
        (path, json.dumps(policy)),
    )


def _stored_raw(conn: sqlite3.Connection, path: str) -> dict | None:
    row = conn.execute("SELECT raw_json FROM projects WHERE path = ?", (path,)).fetchone()
    if row is None:
        return None
    return json.loads(row["raw_json"])


def _write_sync(conn: sqlite3.Connection, path: str, report: dict) -> None:
    conn.execute(
        """
        INSERT INTO sync_log (path, status, applied, events_json, summary, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            path,
            report["status"],
            1 if report.get("applied") else 0,
            json.dumps({"events": report["events"], "virtual_only": report.get("virtual_only") or []}),
            report["summary"],
            _now(),
        ),
    )


def _latest_sync(conn: sqlite3.Connection, path: str) -> dict | None:
    row = conn.execute(
        """
        SELECT status, applied, events_json, summary, created_at
        FROM sync_log WHERE path = ? ORDER BY id DESC LIMIT 1
        """,
        (path,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["events_json"])
    return {
        "status": row["status"],
        "applied": bool(row["applied"]),
        "summary": row["summary"],
        "events": payload.get("events") or [],
        "event_count": len(payload.get("events") or []),
        "virtual_only": payload.get("virtual_only") or [],
        "checked_at": row["created_at"],
    }


def _latest_drift(conn: sqlite3.Connection, path: str) -> dict | None:
    row = conn.execute(
        """
        SELECT events_json, created_at FROM sync_log
        WHERE path = ? AND status = 'drifted' ORDER BY id DESC LIMIT 1
        """,
        (path,),
    ).fetchone()
    if row is None:
        return None
    created = datetime.fromisoformat(row["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
    payload = json.loads(row["events_json"])
    return {"events": payload.get("events") or [], "age_days": age}


def _user_events(conn: sqlite3.Connection, path: str) -> list[dict]:
    rows = conn.execute(
        "SELECT event_json FROM user_events WHERE path = ? ORDER BY id",
        (path,),
    ).fetchall()
    return [json.loads(row["event_json"]) for row in rows]


@contextmanager
def _connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
