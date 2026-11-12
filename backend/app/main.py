"""HTTP API and the single-page twin."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.decide import analyze
from app.decision_intelligence import advise
from app.impact import assess_change
from app.settings import EXAMPLE, FRONTEND, ROOT
from app.simulate import run_counterfactual, run_scenario
from app.store import (
    active_path,
    active_raw,
    current_mission,
    current_view,
    decide_run,
    ensure_ready,
    fail_run,
    fidelity_report,
    ingest,
    inspect_sync,
    learn_run,
    record_check,
    record_event,
    record_loop,
    record_outcome,
    start_run,
    synchronize,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_ready(EXAMPLE)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="AI Engineering Digital Twin", lifespan=lifespan)

    @app.get("/api/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/twin")
    def twin() -> dict:
        try:
            return current_view()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/scan")
    def scan(body: ScanIn) -> dict:
        if body.path:
            root = _resolve_scan_path(body.path)
        else:
            current = active_path()
            if current is None:
                raise HTTPException(400, "No project scanned yet")
            root = Path(current)
        if not root.is_dir():
            raise HTTPException(400, f"Path is not a directory: {root}")
        return ingest(root)

    @app.post("/api/changes/evaluate")
    def evaluate(body: ChangeIn) -> dict:
        try:
            view = current_view()
            return assess_change(view, body.modules, body.title, body.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc

    @app.post("/api/scenarios/run")
    def scenario(body: ScenarioIn) -> dict:
        try:
            raw = active_raw()
            view = current_view()
            return run_scenario(
                raw,
                view["models"],
                body.name,
                [step.model_dump() for step in body.interventions],
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc

    @app.post("/api/decisions")
    def decision(body: DecisionIn) -> dict:
        try:
            raw = active_raw()
            view = current_view()
            report = run_counterfactual(raw, view["models"], body.module)
            width = ((view.get("forecast") or {}).get("by_module") or {}).get(body.module, {}).get("width", 0.12)
            decision = analyze(report, float(width))
            decision["algorithms"] = advise(view, decision)
            return decision
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc

    @app.post("/api/counterfactuals")
    def counterfactual(body: CounterfactualIn) -> dict:
        try:
            raw = active_raw()
            view = current_view()
            return run_counterfactual(raw, view["models"], body.module)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc

    @app.get("/api/run")
    def run_status() -> dict:
        try:
            return current_mission()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/run")
    def run_start() -> dict:
        try:
            return start_run()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/run/approve")
    def run_approve(body: RunDecisionIn) -> dict:
        try:
            return decide_run(body.task, body.approve)
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/run/fail")
    def run_fail(body: RunTaskIn) -> dict:
        try:
            return fail_run(body.task)
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/run/learn")
    def run_learn(body: RunLearnIn) -> dict:
        try:
            return learn_run(body.task, body.happened)
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/fidelity")
    def fidelity() -> dict:
        try:
            return fidelity_report()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/fidelity/checks")
    def fidelity_check(body: CheckIn) -> dict:
        try:
            return record_check(body.module, body.channel, body.passed, body.action, body.note)
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/loop")
    def loop_step(body: LoopIn) -> dict:
        try:
            return record_loop(body.module, body.action, body.happened, body.kind)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc

    @app.post("/api/outcomes")
    def outcome(body: OutcomeIn) -> dict:
        try:
            return record_outcome(body.module, body.kind, body.happened)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'")) from exc

    @app.get("/api/sync")
    def sync_status() -> dict:
        try:
            return inspect_sync()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/sync")
    def sync_apply() -> dict:
        try:
            return synchronize()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/events")
    def event(body: EventIn) -> dict:
        if body.type == "incident" and not body.service:
            raise HTTPException(400, "Incidents need a service")
        payload = body.model_dump()
        if payload.get("module") and not payload.get("files"):
            payload["file"] = payload["module"]
        try:
            return record_event(payload)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/")
    def index() -> FileResponse:
        page = FRONTEND / "index.html"
        if not page.is_file():
            raise HTTPException(404, "UI is not built")
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    @app.get("/assets/{asset_path:path}")
    def assets(asset_path: str) -> FileResponse:
        return FileResponse(_safe_asset(asset_path), headers={"Cache-Control": "no-cache"})

    return app


class ScanIn(BaseModel):
    path: str | None = None


class ChangeIn(BaseModel):
    title: str = "Proposed change"
    note: str = ""
    modules: list[str] = Field(default_factory=list)


class InterventionIn(BaseModel):
    action: Literal["add_tests", "extract_module", "add_owner", "add_circuit_breaker", "freeze"]
    module: str


class DecisionIn(BaseModel):
    module: str


class CounterfactualIn(BaseModel):
    module: str


class ScenarioIn(BaseModel):
    name: str = "Scenario"
    interventions: list[InterventionIn]


class CheckIn(BaseModel):
    module: str
    channel: Literal["build", "test", "production"]
    passed: bool
    action: str = ""
    note: str = ""


class RunTaskIn(BaseModel):
    task: str


class RunDecisionIn(BaseModel):
    task: str
    approve: bool


class RunLearnIn(BaseModel):
    task: str
    happened: bool


class LoopIn(BaseModel):
    module: str
    action: str
    happened: bool
    kind: Literal["bug", "incident", "delay", "perf"] = "bug"


class OutcomeIn(BaseModel):
    module: str
    kind: Literal["bug", "incident", "delay", "perf"] = "bug"
    happened: bool


class EventIn(BaseModel):
    type: Literal["incident", "deploy", "review", "commit"]
    service: str | None = None
    severity: str | None = None
    summary: str = ""
    result: str | None = None
    module: str | None = None
    hours: float | None = None
    author: str | None = None
    age_days: float = 0
    files: dict[str, list[int]] | None = None


def _resolve_scan_path(text: str) -> Path:
    raw = Path(text).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    for base in (ROOT, Path.cwd()):
        candidate = (base / raw).resolve()
        if candidate.exists():
            return candidate
    return (ROOT / raw).resolve()


def _safe_asset(name: str) -> Path:
    root = FRONTEND.resolve()
    candidate = (root / name).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(404, "Not found")
    if not candidate.is_file():
        raise HTTPException(404, "Not found")
    return candidate


app = create_app()
