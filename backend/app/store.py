"""SQLite persistence for scans, beliefs, outcomes, and recorded events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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
