"""FastAPI application for the astrolab control plane.

Endpoints:
- GET  /api/health                       liveness ping
- GET  /api/targets                      target list with frame counts
- GET  /api/targets/{id}                 target detail: sessions + calibration
- GET  /api/sessions/{id}                session detail: frames summary + cal
- POST /api/scan                         trigger a rescan (synchronous)
- GET  /api/masters                      indexed calibration masters
- POST /api/jobs                         submit a Template+Job to run
- GET  /api/jobs                         list known jobs
- GET  /api/jobs/{id}                    job detail (status, outputs, error)
- GET  /api/jobs/{id}/events             buffered events as JSON list
- WS   /api/jobs/{id}/events             live event stream (after replay)

Phase 2 keeps job state in memory; persistence + worker scaling come later.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

import nodes.basic  # noqa: F401  registers nodes for job execution
import server.catalog.adapters  # noqa: F401  registers ingest adapters
from server.catalog.common_names import lookup as lookup_common_name
from server.catalog.db import open_db
from server.catalog.scanner import scan as run_scan
from server.job_builder import (
    CalibrationMissing,
    JobBuildError,
    SessionNotFound,
    TooFewFrames,
    build_from_session,
)
from server.jobs import JobManager
from server.models import CalibrationSpec, Job, Template
from server.preview import PreviewError, render_preview
from server.templates import TemplateNotFound, list_templates, load_template

log = logging.getLogger("astrolab.api")

job_manager = JobManager()


from contextlib import asynccontextmanager  # noqa: E402  (used by app() below)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Pull persisted jobs into memory so /jobs lists them and the detail page
    # can replay events even after a server restart.
    try:
        job_manager.rehydrate()
    except Exception:  # pragma: no cover  (defensive: server starts even if DB is wedged)
        log.exception("job rehydrate failed; continuing with empty state")
    yield
    job_manager.shutdown(wait=False)


app = FastAPI(title="astrolab", version="0.1.0", lifespan=lifespan)

# Phase 1 dev: SvelteKit dev server runs on a different port. Allow it through
# CORS. Tighten or drop once the static build is mounted under the same origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,
)


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    with open_db() as conn:
        yield conn


def db_dep() -> Iterator[sqlite3.Connection]:
    with _db() as conn:
        yield conn


DBDep = Annotated[sqlite3.Connection, Depends(db_dep)]


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class TargetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    common_name: str | None = None
    session_count: int
    frame_count: int
    failed_count: int
    last_session_at: str | None = None


class CalibrationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    quality: str
    master_id: int | None = None


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    session_key: str
    target_name: str | None = None
    instrument: str | None = None
    camera: str | None = None
    filter: str | None = None
    exptime: float | None = None
    gain: int | None = None
    binning: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    frame_count: int
    failed_count: int
    calibration: list[CalibrationStatus]


class TargetDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    common_name: str | None = None
    sessions: list[SessionSummary]


class MasterRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    kind: str
    instrument: str | None
    camera: str | None
    filter: str | None
    exptime: float | None
    gain: int | None
    binning: int | None
    ccd_temp: float | None
    stack_count: int | None
    source: str | None
    path: str


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str
    scope_id: str = "dwarf3"


class ScanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovered: int
    inserted: int
    updated: int
    removed: int
    skipped_unchanged: int
    failed: int
    masters_inserted: int
    masters_updated: int
    masters_removed: int
    masters_skipped: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _calibration_for_session(
    conn: sqlite3.Connection, session_id: int
) -> list[CalibrationStatus]:
    rows = conn.execute(
        "SELECT kind, master_id, match_quality FROM calibration_matches "
        "WHERE session_id = ? ORDER BY kind",
        (session_id,),
    ).fetchall()
    return [
        CalibrationStatus(
            kind=r["kind"], quality=r["match_quality"], master_id=r["master_id"]
        )
        for r in rows
    ]


def _row_to_session_summary(
    conn: sqlite3.Connection, row: sqlite3.Row, target_name: str | None
) -> SessionSummary:
    return SessionSummary(
        id=row["id"],
        session_key=row["session_key"],
        target_name=target_name,
        instrument=row["instrument"],
        camera=row["camera"],
        filter=row["filter"],
        exptime=row["exptime"],
        gain=row["gain"],
        binning=row["binning"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        frame_count=row["frame_count"] or 0,
        failed_count=row["failed_count"] or 0,
        calibration=_calibration_for_session(conn, row["id"]),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/targets", response_model=list[TargetSummary])
def list_targets(conn: DBDep) -> list[TargetSummary]:
    rows = conn.execute(
        """
        SELECT t.id, t.name,
               COUNT(DISTINCT s.id)        AS session_count,
               IFNULL(SUM(s.frame_count), 0) AS frame_count,
               IFNULL(SUM(s.failed_count), 0) AS failed_count,
               MAX(s.started_at)             AS last_session_at
        FROM targets t
        LEFT JOIN sessions s ON s.target_id = t.id
        GROUP BY t.id, t.name
        ORDER BY t.name
        """
    ).fetchall()
    return [
        TargetSummary(
            id=r["id"],
            name=r["name"],
            common_name=lookup_common_name(r["name"]),
            session_count=r["session_count"],
            frame_count=r["frame_count"],
            failed_count=r["failed_count"],
            last_session_at=r["last_session_at"],
        )
        for r in rows
    ]


@app.get("/api/targets/{target_id}", response_model=TargetDetail)
def get_target(
    target_id: int, conn: DBDep
) -> TargetDetail:
    target = conn.execute(
        "SELECT id, name FROM targets WHERE id = ?", (target_id,)
    ).fetchone()
    if target is None:
        raise HTTPException(status_code=404, detail=f"target {target_id} not found")

    sessions = conn.execute(
        """
        SELECT * FROM sessions
        WHERE target_id = ?
        ORDER BY started_at
        """,
        (target_id,),
    ).fetchall()
    return TargetDetail(
        id=target["id"],
        name=target["name"],
        common_name=lookup_common_name(target["name"]),
        sessions=[
            _row_to_session_summary(conn, s, target_name=target["name"])
            for s in sessions
        ],
    )


@app.get("/api/sessions/{session_id}", response_model=SessionSummary)
def get_session(
    session_id: int, conn: DBDep
) -> SessionSummary:
    row = conn.execute(
        """
        SELECT s.*, t.name AS target_name
        FROM sessions s LEFT JOIN targets t ON s.target_id = t.id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    return _row_to_session_summary(conn, row, target_name=row["target_name"])


@app.get("/api/masters", response_model=list[MasterRow])
def list_masters(conn: DBDep) -> list[MasterRow]:
    rows = conn.execute(
        "SELECT id, kind, instrument, camera, filter, exptime, gain, binning, "
        "ccd_temp, stack_count, source, path "
        "FROM masters ORDER BY kind, exptime, ccd_temp"
    ).fetchall()
    return [MasterRow(**dict(r)) for r in rows]


@app.post("/api/scan", response_model=ScanResponse)
def trigger_scan(req: ScanRequest) -> ScanResponse:
    root = Path(req.root).expanduser()
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"root does not exist: {root}")
    log.info("scan triggered: scope=%s root=%s", req.scope_id, root)
    stats = run_scan(root, scope_id=req.scope_id)
    return ScanResponse(
        discovered=stats.discovered,
        inserted=stats.inserted,
        updated=stats.updated,
        removed=stats.removed,
        skipped_unchanged=stats.skipped_unchanged,
        failed=stats.failed,
        masters_inserted=stats.masters_inserted,
        masters_updated=stats.masters_updated,
        masters_removed=stats.masters_removed,
        masters_skipped=stats.masters_skipped,
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class SubmitJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: Template
    job: Job


class SubmitJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str


@app.post("/api/jobs", response_model=SubmitJobResponse)
def submit_job(req: SubmitJobRequest) -> SubmitJobResponse:
    job_id = job_manager.submit(req.template, req.job)
    log.info("job submitted: %s template=%s", job_id, req.template.id)
    return SubmitJobResponse(job_id=job_id)


@app.get("/api/templates")
def list_templates_endpoint() -> list[dict]:
    return [t.model_dump(mode="json") for t in list_templates()]


class SubmitFromSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: int
    template_id: str
    calibration: CalibrationSpec | None = None


@app.post("/api/jobs/from_session", response_model=SubmitJobResponse)
def submit_from_session(req: SubmitFromSessionRequest, conn: DBDep) -> SubmitJobResponse:
    """Build a Job from a catalog session and submit it.

    Resolves the session's lights folder + matched master (per calibration
    spec) into the template's external inputs. Surfaces 404 when session or
    template is unknown, 400 when calibration can't be resolved.
    """
    try:
        template = load_template(req.template_id)
    except TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail=f"template {exc} not found") from exc
    try:
        job = build_from_session(conn, req.session_id, template, req.calibration)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CalibrationMissing, TooFewFrames) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBuildError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = job_manager.submit(template, job)
    log.info(
        "job submitted from session: %s session=%s template=%s",
        job_id, req.session_id, req.template_id,
    )
    return SubmitJobResponse(job_id=job_id)


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    # Newest-first; in-memory for now so a quick list is fine.
    return [r.public_dict() for r in sorted(
        job_manager.list_jobs(), key=lambda r: r.submitted_at, reverse=True
    )]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    # Detail view includes the template so the UI can render the DAG.
    return record.public_dict(include_template=True)


@app.get("/api/jobs/{job_id}/events")
def get_job_events(job_id: str) -> list[dict]:
    """Return the buffered event history for a job (snapshot, not live)."""
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return [ev.to_dict() for ev in record.events]


@app.websocket("/api/jobs/{job_id}/events")
async def stream_job_events(ws: WebSocket, job_id: str) -> None:
    """Live event stream for a job: replays history, then streams new events.

    Closes when the job reaches a terminal state (completed or failed). The
    client can reconnect or fall back to GET /api/jobs/{id} for the snapshot.
    """
    await ws.accept()
    queue = job_manager.subscribe(job_id)
    if queue is None:
        await ws.close(code=4404, reason=f"job {job_id} not found")
        return
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event.to_dict())
            if event.type in ("job_completed", "job_failed"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        job_manager.unsubscribe(job_id, queue)
        # Best-effort close; ignore if already closed.
        with contextlib.suppress(RuntimeError):
            await ws.close()


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------


@app.get("/api/preview/{node_hash}/{port}")
def get_preview(node_hash: str, port: str) -> FileResponse:
    """Render (or return cached) thumbnail PNG for a node's output.

    The preview is cached inside the node's cache entry so subsequent loads
    are a static file read. FITS artifacts get an asinh-stretched render;
    PNG artifacts pass through.
    """
    try:
        path = render_preview(job_manager.cache, node_hash, port)
    except PreviewError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="image/png",
        # The cache is content-addressed, so a hit is permanent and cacheable.
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# Make `python -m server.api` start a dev server.
def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("server.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
