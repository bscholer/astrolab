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
from typing import Annotated, Any

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
from server.registry import lookup as registry_lookup
from server.renderings import RenderingManager, RenderingNotFound
from server.templates import TemplateNotFound, list_templates, load_template

log = logging.getLogger("astrolab.api")

job_manager = JobManager()
rendering_manager = RenderingManager(job_manager)


from contextlib import asynccontextmanager  # noqa: E402  (used by app() below)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Pull persisted jobs and renderings into memory so the UI lists them
    # and detail pages can replay events even after a server restart.
    try:
        job_manager.rehydrate()
    except Exception:  # pragma: no cover  (defensive: server starts even if DB is wedged)
        log.exception("job rehydrate failed; continuing with empty state")
    try:
        rendering_manager.rehydrate()
    except Exception:  # pragma: no cover
        log.exception("rendering rehydrate failed; continuing with empty state")
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
    allow_methods=["GET", "POST", "PATCH"],
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
    reason: str | None = None
    """Matcher's explanation, surfaced to the UI so users see why a session
    has no calibration before they try to Run it. Sourced from
    calibration_matches.details (JSON blob); we extract the 'reason' key when
    present and ignore the rest."""


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
        "SELECT kind, master_id, match_quality, details FROM calibration_matches "
        "WHERE session_id = ? ORDER BY kind",
        (session_id,),
    ).fetchall()
    out: list[CalibrationStatus] = []
    for r in rows:
        reason: str | None = None
        if r["details"]:
            try:
                import json as _json
                payload = _json.loads(r["details"])
                if isinstance(payload, dict):
                    reason = payload.get("reason") or None
            except (ValueError, TypeError):
                pass
        out.append(
            CalibrationStatus(
                kind=r["kind"],
                quality=r["match_quality"],
                master_id=r["master_id"],
                reason=reason,
            )
        )
    return out


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


@app.post("/api/jobs/{job_id}/rerun", response_model=SubmitJobResponse)
def rerun_job(job_id: str) -> SubmitJobResponse:
    """Reprocess: submit a fresh copy of an existing job with cache bypass.

    The original job stays in the list as history; the new job runs every
    node from scratch, then commits results back so subsequent non-force
    runs against the same inputs will hit the rebuilt cache.
    """
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    new_id = job_manager.submit(record.template, record.job, force=True)
    log.info("job rerun: %s -> %s template=%s", job_id, new_id, record.template.id)
    return SubmitJobResponse(job_id=new_id)


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
        log.warning("from_session rejected: unknown template %r", req.template_id)
        raise HTTPException(status_code=404, detail=f"template {exc} not found") from exc
    try:
        job = build_from_session(conn, req.session_id, template, req.calibration)
    except SessionNotFound as exc:
        log.warning("from_session rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CalibrationMissing, TooFewFrames) as exc:
        log.warning("from_session rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBuildError as exc:
        log.warning("from_session rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = job_manager.submit(template, job)
    log.info(
        "job submitted from session: %s session=%s template=%s",
        job_id, req.session_id, req.template_id,
    )
    return SubmitJobResponse(job_id=job_id)


def _job_capture_summary(conn: sqlite3.Connection, session_ids: list[str]) -> dict:
    """Pull target / frame_count / exptime / gain / camera / filter for the
    sessions a job ran against, for header display in the UI.

    Jobs not built via from_session (eg the smoke pipeline) have empty
    session_ids; those return {} so the UI falls back to template metadata.
    """
    if not session_ids:
        return {}
    try:
        ids = [int(sid) for sid in session_ids]
    except (ValueError, TypeError):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT s.id, s.frame_count, s.failed_count, s.exptime, s.gain, s.binning,
               s.camera, s.filter, t.name AS target_name
        FROM sessions s
        LEFT JOIN targets t ON t.id = s.target_id
        WHERE s.id IN ({placeholders})
        """,  # noqa: S608  (placeholders are ints)
        ids,
    ).fetchall()
    if not rows:
        return {}
    target_names = sorted({r["target_name"] for r in rows if r["target_name"]})
    frame_count = sum((r["frame_count"] or 0) for r in rows)
    failed_count = sum((r["failed_count"] or 0) for r in rows)
    # Sessions a single job spans usually share these; show the first.
    first = rows[0]
    return {
        "target_name": ", ".join(target_names) or None,
        "frame_count": frame_count,
        "failed_count": failed_count,
        "session_count": len(rows),
        "exptime": first["exptime"],
        "gain": first["gain"],
        "binning": first["binning"],
        "camera": first["camera"],
        "filter": first["filter"],
    }


def _enriched_job_dict(
    record, conn: sqlite3.Connection, *, include_template: bool = False
) -> dict:
    d = record.public_dict(include_template=include_template)
    summary = _job_capture_summary(conn, record.job.session_ids)
    if summary:
        d["capture"] = summary
    return d


@app.get("/api/jobs")
def list_jobs(conn: DBDep) -> list[dict]:
    # Newest-first; in-memory for now so a quick list is fine.
    return [
        _enriched_job_dict(r, conn)
        for r in sorted(
            job_manager.list_jobs(), key=lambda r: r.submitted_at, reverse=True
        )
    ]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, conn: DBDep) -> dict:
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    # Detail view includes the template so the UI can render the DAG.
    return _enriched_job_dict(record, conn, include_template=True)


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
# Renderings
# ---------------------------------------------------------------------------


class CreateRenderingRequest(BaseModel):
    """Generic 'rendering from an explicit Template + Job' creator.

    Mirrors POST /api/jobs; used by smoke scripts and tests that don't want
    to go through the catalog. Production UI uses /api/renderings/from_session.
    """

    model_config = ConfigDict(extra="forbid")
    template: Template
    job: Job
    name: str = "untitled"
    source_session_ids: list[str] = []


class CreateRenderingFromSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: int
    template_id: str
    name: str | None = None
    """Human-friendly title; defaults to the session's target name."""
    calibration: CalibrationSpec | None = None


@app.post("/api/renderings")
def create_rendering(req: CreateRenderingRequest) -> dict:
    rendering = rendering_manager.create(
        name=req.name,
        template=req.template,
        base_job=req.job,
        source_session_ids=req.source_session_ids,
    )
    return rendering.to_public_dict()


class RenderingResponse(BaseModel):
    """Loose passthrough so we don't have to re-spec the whole DTO here. The
    rendering manager produces a stable shape via to_public_dict()."""

    model_config = ConfigDict(extra="allow")


class PatchRenderingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overrides: dict[str, dict[str, Any] | None] | None = None
    """Partial overrides keyed by node_id. Pass {node_id: None} to clear a
    node entirely; {node_id: {param: None}} to reset a single param."""
    draft_mode: bool | None = None
    label: str | None = None
    """Optional explicit label for this history entry; auto-generated from
    the diff when omitted."""
    force: bool = False
    """Bypass the cache for this submission (debug rerun)."""


@app.post("/api/renderings/from_session")
def create_rendering_from_session(
    req: CreateRenderingFromSessionRequest, conn: DBDep
) -> dict:
    """Create a Rendering from a catalog session and submit its initial job."""
    try:
        template = load_template(req.template_id)
    except TemplateNotFound as exc:
        log.warning("rendering create rejected: unknown template %r", req.template_id)
        raise HTTPException(status_code=404, detail=f"template {exc} not found") from exc
    try:
        job = build_from_session(conn, req.session_id, template, req.calibration)
    except SessionNotFound as exc:
        log.warning("rendering create rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CalibrationMissing, TooFewFrames) as exc:
        log.warning("rendering create rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBuildError as exc:
        log.warning("rendering create rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    name = req.name
    if not name:
        # Default to the session's target name so the rendering shows up in
        # the list with a meaningful title.
        row = conn.execute(
            "SELECT t.name FROM sessions s LEFT JOIN targets t ON t.id = s.target_id "
            "WHERE s.id = ?",
            (req.session_id,),
        ).fetchone()
        name = (row["name"] if row and row["name"] else f"session {req.session_id}")

    rendering = rendering_manager.create(
        name=name,
        template=template,
        base_job=job,
        source_session_ids=[str(req.session_id)],
    )
    return rendering.to_public_dict()


@app.get("/api/renderings")
def list_renderings() -> list[dict]:
    out = [r.to_public_dict() for r in rendering_manager.list()]
    out.sort(key=lambda r: r["updated_at"], reverse=True)
    return out


@app.get("/api/renderings/{rendering_id}")
def get_rendering(rendering_id: str) -> dict:
    rendering = rendering_manager.get(rendering_id)
    if rendering is None:
        raise HTTPException(status_code=404, detail=f"rendering {rendering_id} not found")
    return rendering.to_public_dict()


@app.patch("/api/renderings/{rendering_id}")
def patch_rendering(rendering_id: str, req: PatchRenderingRequest) -> dict:
    """Apply param overrides (and optionally toggle draft_mode), submit a new
    job, and append a history entry. Returns the updated rendering."""
    try:
        rendering = rendering_manager.patch(
            rendering_id,
            overrides=req.overrides,
            draft_mode=req.draft_mode,
            label=req.label,
            force=req.force,
        )
    except RenderingNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"rendering {rendering_id} not found"
        ) from exc
    return rendering.to_public_dict()


@app.post("/api/renderings/{rendering_id}/revert/{seq}")
def revert_rendering(rendering_id: str, seq: int) -> dict:
    """Move the current pointer to history seq `seq`. No new job; the prior
    history entry's job_id is what the UI displays."""
    try:
        rendering = rendering_manager.revert(rendering_id, seq)
    except RenderingNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"rendering {rendering_id} not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return rendering.to_public_dict()


# ---------------------------------------------------------------------------
# Template + node schema
# ---------------------------------------------------------------------------


@app.get("/api/templates/{template_id}/schema")
def get_template_schema(template_id: str) -> dict:
    """Return per-node parameter JSON schemas + cost class for a template.

    The UI uses this to auto-build param forms with cost-aware affordances.
    Each entry mirrors the template's NodeSpec but adds the Pydantic
    JSON-Schema (with descriptions, ge/le, enums, defaults) and the node
    class's cost label. Downstream-closure cost is derived in the UI from
    the template's edge graph.
    """
    try:
        template = load_template(template_id)
    except TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail=f"template {exc} not found") from exc

    nodes_out: list[dict] = []
    for spec in template.nodes:
        try:
            node_cls = registry_lookup(spec.kind, spec.variant)
        except KeyError as exc:
            raise HTTPException(
                status_code=500, detail=f"unknown node kind in template: {exc}"
            ) from exc
        schema = node_cls.params_schema.model_json_schema()
        defaults = node_cls.params_schema().model_dump(mode="json")
        nodes_out.append(
            {
                "node_id": spec.id,
                "kind": spec.kind,
                "variant": spec.variant,
                "cost": node_cls.cost,
                "schema": schema,
                "defaults": defaults,
                "template_params": spec.params,
                "inputs": spec.inputs,
            }
        )
    return {
        "template_id": template.id,
        "template_version": template.version,
        "nodes": nodes_out,
        "outputs": template.outputs,
    }


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
        # Content-addressed by node_hash, but the *renderer* can change (siril
        # autostretch swap, MTF tweaks, etc.). max-age covers a normal session
        # and dropping `immutable` lets a hard refresh actually fetch the new
        # render after a code change.
        headers={"Cache-Control": "public, max-age=60"},
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
