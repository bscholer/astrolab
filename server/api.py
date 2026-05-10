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
- POST /api/projects                     create a project from Template+Job
- POST /api/projects/from_session        create a project from a catalog session
- GET  /api/projects                     list projects (newest first)
- GET  /api/projects/{id}                project detail (state + history)
- PATCH /api/projects/{id}               apply param overrides / draft toggle
- POST /api/projects/{id}/revert/{seq}   move history pointer
- DELETE /api/projects/{id}              delete project + owned cache
- DELETE /api/projects/{id}/cache        purge owned cache (keep_outputs?)
- GET  /api/storage                      cache size + per-project breakdown
- POST /api/storage/cleanup              run eviction sweep
- GET  /api/settings                     read system settings
- PATCH /api/settings                    update settings (cache_max_bytes)

Phase 2 keeps job state in memory; persistence + worker scaling come later.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

import nodes.basic  # noqa: F401  registers nodes for job execution
import server.catalog.adapters  # noqa: F401  registers ingest adapters
from server.catalog.common_names import lookup as lookup_common_name
from server.catalog.db import open_db
from server.catalog.openngc import enrich as openngc_enrich
from server.catalog.scanner import scan as run_scan
from server.job_builder import (
    CalibrationMissing,
    IncompatibleSessions,
    JobBuildError,
    SessionNotFound,
    TooFewFrames,
    build_from_session,
    build_from_sessions,
)
from server.jobs import JobManager
from server.models import CalibrationSpec, Job, Template
from server.preview import PreviewError, render_preview
from server.projects import ProjectManager, ProjectNotFound
from server.registry import lookup as registry_lookup
from server.storage import (
    MIN_CACHE_MAX_BYTES,
    SETTING_CACHE_MAX_BYTES,
    SETTING_CACHE_ROOT_OVERRIDE,
    SETTING_CAPTURE_ROOT,
    default_cache_max_bytes_for,
    delete_project,
    get_setting,
    purge_project_cache,
    run_cleanup,
    set_setting,
    system_storage,
)
from server.templates import TemplateNotFound, list_templates, load_template

log = logging.getLogger("astrolab.api")

job_manager = JobManager()
project_manager = ProjectManager(job_manager)


from contextlib import asynccontextmanager  # noqa: E402  (used by app() below)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Pull persisted jobs and projects into memory so the UI lists them
    # and detail pages can replay events even after a server restart.
    try:
        job_manager.rehydrate()
    except Exception:  # pragma: no cover  (defensive: server starts even if DB is wedged)
        log.exception("job rehydrate failed; continuing with empty state")
    try:
        project_manager.rehydrate()
    except Exception:  # pragma: no cover
        log.exception("project rehydrate failed; continuing with empty state")
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
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
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


class SkyInfo(BaseModel):
    """Sub-payload bundled into target responses when we recognize the
    catalog id. Pulled from the OpenNGC vendored CSVs at boot time;
    units are decimal degrees (J2000), V-band magnitude preferred."""

    model_config = ConfigDict(extra="forbid")

    ra_deg: float | None = None
    dec_deg: float | None = None
    magnitude: float | None = None
    constellation: str | None = None
    object_type: str | None = None


def _resolve_target_meta(name: str) -> tuple[str | None, SkyInfo | None]:
    """Look the target's catalog id up in OpenNGC. Falls back to the
    curated common_names.json for any custom entries OpenNGC doesn't
    cover. Returns (common_name, sky_info)."""
    entry = openngc_enrich(name)
    if entry is not None:
        sky = SkyInfo(
            ra_deg=entry.ra_deg,
            dec_deg=entry.dec_deg,
            magnitude=entry.magnitude,
            constellation=entry.constellation,
            object_type=entry.object_type,
        )
        # If OpenNGC has no friendly name for this row, fall back to the
        # curated table (e.g. local nicknames or names we patched in).
        return entry.common_name or lookup_common_name(name), sky
    return lookup_common_name(name), None


class TargetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    common_name: str | None = None
    sky: SkyInfo | None = None
    session_count: int
    frame_count: int
    failed_count: int
    last_session_at: str | None = None
    # Useful integration time across all the target's sessions:
    # sum((frame_count - failed_count) * exptime). Null when no session
    # has both a frame count and an exposure time on file.
    integration_seconds: float | None = None
    # Total bytes the target's frames occupy on disk. Computed from the
    # frames table's `size` column, joined via session_key so we don't
    # double-count when frames are shared across sessions.
    bytes_on_disk: int = 0


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
    # Useful integration time = (frame_count - failed_count) * exptime.
    # Null when exptime isn't known.
    integration_seconds: float | None = None
    # SUM(frames.size) for frames whose session_key matches.
    bytes_on_disk: int = 0
    calibration: list[CalibrationStatus]


class TargetDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    common_name: str | None = None
    sky: SkyInfo | None = None
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
    # Bytes are the on-disk size of all frames sharing this session_key.
    # Cheap one-row scalar; the session list pages don't fan out wide
    # enough for this to be a problem (typical user has dozens of
    # sessions, not thousands).
    size_row = conn.execute(
        "SELECT IFNULL(SUM(size), 0) AS bytes FROM frames WHERE session_key = ?",
        (row["session_key"],),
    ).fetchone()
    bytes_on_disk = int(size_row["bytes"] if size_row else 0)
    frame_count = row["frame_count"] or 0
    failed_count = row["failed_count"] or 0
    exptime = row["exptime"]
    integration: float | None = None
    if exptime is not None:
        usable = max(0, frame_count - failed_count)
        integration = float(exptime) * usable if usable > 0 else None
    return SessionSummary(
        id=row["id"],
        session_key=row["session_key"],
        target_name=target_name,
        instrument=row["instrument"],
        camera=row["camera"],
        filter=row["filter"],
        exptime=exptime,
        gain=row["gain"],
        binning=row["binning"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        frame_count=frame_count,
        failed_count=failed_count,
        integration_seconds=integration,
        bytes_on_disk=bytes_on_disk,
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
    # Integration time is summed at the session level so a session
    # without exptime contributes 0 instead of NULL-poisoning the
    # whole row. Bytes are computed in a separate scalar query so the
    # frame join doesn't blow up the per-target session count.
    rows = conn.execute(
        """
        SELECT t.id, t.name,
               COUNT(DISTINCT s.id)        AS session_count,
               IFNULL(SUM(s.frame_count), 0) AS frame_count,
               IFNULL(SUM(s.failed_count), 0) AS failed_count,
               MAX(s.started_at)             AS last_session_at,
               IFNULL(SUM(
                 CASE WHEN s.exptime IS NOT NULL
                      THEN MAX(0, IFNULL(s.frame_count,0) - IFNULL(s.failed_count,0))
                           * s.exptime
                      ELSE 0 END
               ), 0) AS integration_seconds,
               (
                 SELECT IFNULL(SUM(f.size), 0)
                 FROM frames f
                 JOIN sessions s2 ON s2.session_key = f.session_key
                 WHERE s2.target_id = t.id
               ) AS bytes_on_disk
        FROM targets t
        LEFT JOIN sessions s ON s.target_id = t.id
        GROUP BY t.id, t.name
        ORDER BY t.name
        """
    ).fetchall()
    summaries: list[TargetSummary] = []
    for r in rows:
        common, sky = _resolve_target_meta(r["name"])
        # 0 from the query means "nothing to integrate" — surface as None
        # so the UI can show '—' instead of '0s'.
        integ = float(r["integration_seconds"]) or None
        summaries.append(
            TargetSummary(
                id=r["id"],
                name=r["name"],
                common_name=common,
                sky=sky,
                session_count=r["session_count"],
                frame_count=r["frame_count"],
                failed_count=r["failed_count"],
                last_session_at=r["last_session_at"],
                integration_seconds=integ,
                bytes_on_disk=int(r["bytes_on_disk"] or 0),
            )
        )
    return summaries


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
    common, sky = _resolve_target_meta(target["name"])
    return TargetDetail(
        id=target["id"],
        name=target["name"],
        common_name=common,
        sky=sky,
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
            if event.type in ("job_completed", "job_failed", "job_interrupted"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        job_manager.unsubscribe(job_id, queue)
        # Best-effort close; ignore if already closed.
        with contextlib.suppress(RuntimeError):
            await ws.close()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class CreateProjectRequest(BaseModel):
    """Generic 'project from an explicit Template + Job' creator.

    Mirrors POST /api/jobs; used by smoke scripts and tests that don't want
    to go through the catalog. Production UI uses /api/projects/from_session.
    """

    model_config = ConfigDict(extra="forbid")
    template: Template
    job: Job
    name: str = "untitled"
    source_session_ids: list[str] = []


class CreateProjectFromSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: int
    template_id: str
    name: str | None = None
    """Human-friendly title; defaults to the session's target name."""
    calibration: CalibrationSpec | None = None


class CreateProjectFromSessionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_ids: list[int]
    """One or more catalog sessions, all sharing target/gain/exptime/filter.
    Order is normalized server-side; the bundle hits the same cache lineage
    regardless of selection order."""
    template_id: str
    name: str | None = None
    calibration: CalibrationSpec | None = None


@app.post("/api/projects")
def create_project(req: CreateProjectRequest) -> dict:
    project = project_manager.create(
        name=req.name,
        template=req.template,
        base_job=req.job,
        source_session_ids=req.source_session_ids,
    )
    return project.to_public_dict()


class PatchProjectRequest(BaseModel):
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


@app.post("/api/projects/from_session")
def create_project_from_session(
    req: CreateProjectFromSessionRequest, conn: DBDep
) -> dict:
    """Create a Project from a catalog session and submit its initial job."""
    try:
        template = load_template(req.template_id)
    except TemplateNotFound as exc:
        log.warning("project create rejected: unknown template %r", req.template_id)
        raise HTTPException(status_code=404, detail=f"template {exc} not found") from exc
    try:
        job = build_from_session(conn, req.session_id, template, req.calibration)
    except SessionNotFound as exc:
        log.warning("project create rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CalibrationMissing, TooFewFrames) as exc:
        log.warning("project create rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBuildError as exc:
        log.warning("project create rejected (session=%s): %s", req.session_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    name = req.name
    if not name:
        # Default to the session's target name so the project shows up in
        # the list with a meaningful title.
        row = conn.execute(
            "SELECT t.name FROM sessions s LEFT JOIN targets t ON t.id = s.target_id "
            "WHERE s.id = ?",
            (req.session_id,),
        ).fetchone()
        name = (row["name"] if row and row["name"] else f"session {req.session_id}")

    project = project_manager.create(
        name=name,
        template=template,
        base_job=job,
        source_session_ids=[str(req.session_id)],
    )
    return project.to_public_dict()


@app.post("/api/projects/from_sessions")
def create_project_from_sessions(
    req: CreateProjectFromSessionsRequest, conn: DBDep
) -> dict:
    """Create a Project that stacks multiple compatible catalog sessions.

    Compatibility rule: every session must share target/instrument/camera/
    filter/exptime/gain/binning. The job builder enforces this and returns
    a 400 with the offending fields named when it doesn't hold. Sessions
    are de-duplicated and order-normalized so [3,1] and [1,3] hit the same
    cache lineage.
    """
    if not req.session_ids:
        raise HTTPException(
            status_code=400, detail="session_ids must not be empty"
        )
    # build_from_sessions normalizes order and de-dupes internally; we just
    # mirror that here so the project record stores the canonical list.
    sids = sorted(set(req.session_ids))

    try:
        template = load_template(req.template_id)
    except TemplateNotFound as exc:
        log.warning("project create rejected: unknown template %r", req.template_id)
        raise HTTPException(status_code=404, detail=f"template {exc} not found") from exc
    try:
        job = build_from_sessions(conn, sids, template, req.calibration)
    except SessionNotFound as exc:
        log.warning("project create rejected (sessions=%s): %s", sids, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CalibrationMissing, TooFewFrames, IncompatibleSessions) as exc:
        log.warning("project create rejected (sessions=%s): %s", sids, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBuildError as exc:
        log.warning("project create rejected (sessions=%s): %s", sids, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    name = req.name
    if not name:
        row = conn.execute(
            "SELECT t.name FROM sessions s LEFT JOIN targets t ON t.id = s.target_id "
            "WHERE s.id = ?",
            (sids[0],),
        ).fetchone()
        target_name = row["name"] if row and row["name"] else None
        if target_name:
            # Multi-session project name reads naturally with the count: the
            # gallery view de-dupes by target anyway, so a single-target
            # project named "M 33 (3 sessions)" is unambiguous.
            name = (
                f"{target_name} ({len(sids)} sessions)"
                if len(sids) > 1
                else target_name
            )
        else:
            name = f"sessions {sids}"

    project = project_manager.create(
        name=name,
        template=template,
        base_job=job,
        source_session_ids=[str(s) for s in sids],
    )
    return project.to_public_dict()


class ProjectCapture(BaseModel):
    """Aggregate of the source sessions a project was built from. Surfaced
    on /api/projects so each row in the Projects list can show frame
    counts, exposure, gain, filter, and date range without the UI having
    to fan out per-session lookups."""

    model_config = ConfigDict(extra="forbid")

    session_count: int = 0
    frame_count: int = 0
    failed_count: int = 0
    exptime: float | None = None
    gain: int | None = None
    filter: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    target_name: str | None = None
    target_common_name: str | None = None
    # Useful integration time across the project's sessions:
    # sum((frame_count - failed_count) * exptime) per session. Null when
    # no source session has both a frame count and an exposure.
    integration_seconds: float | None = None
    # Total bytes of all frames belonging to source sessions.
    bytes_on_disk: int = 0


def _capture_for_project(conn: sqlite3.Connection, session_ids: list[str]) -> ProjectCapture:
    """Aggregate the project's source sessions into a single capture
    line. exptime/gain/filter are taken from the first session — typical
    projects are built from one session, so the simple read is right
    almost always; multi-session aggregations would need richer UI to
    render mixed values anyway."""
    if not session_ids:
        return ProjectCapture()
    try:
        ids = [int(s) for s in session_ids]
    except ValueError:
        return ProjectCapture()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT s.session_key, s.exptime, s.gain, s.filter,
               s.frame_count, s.failed_count,
               s.started_at, s.ended_at, t.name AS target_name
        FROM sessions s
        LEFT JOIN targets t ON t.id = s.target_id
        WHERE s.id IN ({placeholders})
        ORDER BY s.started_at
        """,
        ids,
    ).fetchall()
    if not rows:
        return ProjectCapture()
    head = rows[0]
    target_name = head["target_name"]
    common_name, _ = _resolve_target_meta(target_name) if target_name else (None, None)

    integration_total = 0.0
    integration_seen = False
    for r in rows:
        if r["exptime"] is None:
            continue
        usable = max(0, (r["frame_count"] or 0) - (r["failed_count"] or 0))
        if usable <= 0:
            continue
        integration_total += float(r["exptime"]) * usable
        integration_seen = True

    keys = [r["session_key"] for r in rows if r["session_key"]]
    bytes_on_disk = 0
    if keys:
        ph = ",".join("?" for _ in keys)
        size_row = conn.execute(
            f"SELECT IFNULL(SUM(size), 0) AS bytes FROM frames "
            f"WHERE session_key IN ({ph})",
            keys,
        ).fetchone()
        bytes_on_disk = int(size_row["bytes"] if size_row else 0)

    return ProjectCapture(
        session_count=len(rows),
        frame_count=sum((r["frame_count"] or 0) for r in rows),
        failed_count=sum((r["failed_count"] or 0) for r in rows),
        exptime=head["exptime"],
        gain=head["gain"],
        filter=head["filter"],
        started_at=min((r["started_at"] for r in rows if r["started_at"]), default=None),
        ended_at=max((r["ended_at"] for r in rows if r["ended_at"]), default=None),
        target_name=target_name,
        target_common_name=common_name,
        integration_seconds=integration_total if integration_seen else None,
        bytes_on_disk=bytes_on_disk,
    )


def _attach_preview(project_dict: dict) -> dict:
    """Resolve a (preview_hash, preview_port) pair for the project so
    the UI can render a thumbnail without a second roundtrip.

    Walks history newest-to-oldest, starting at current_seq, and picks
    the first job with outputs. This means a failed retweak still
    shows the last good render — important because the projects list
    is the user's at-a-glance "what does each of my stacks look like"
    view, not a status board.

    Picks the same output the project detail page uses: the port
    named 'image' if present, otherwise the first output. Leaves the
    fields off when nothing in history has outputs yet — UI falls
    back to an inline version chip in that case.
    """
    history = project_dict.get("history") or []
    current_seq = project_dict.get("current_seq", 0)
    cover_seq = project_dict.get("cover_seq")
    # If the user pinned a cover, try it first. Then fall back through
    # the auto-pick chain (current pointer, then earlier history, then
    # later) so a stale cover (e.g. its cache was evicted) still
    # produces a thumbnail instead of an empty slot.
    seq_order: list[int] = []
    if cover_seq is not None:
        seq_order.append(cover_seq)
    seq_order.extend(
        [current_seq]
        + list(range(current_seq - 1, -1, -1))
        + list(range(current_seq + 1, len(history)))
    )
    by_seq = {h["seq"]: h for h in history}
    for seq in seq_order:
        entry = by_seq.get(seq)
        if entry is None:
            continue
        record = job_manager.get(entry["job_id"])
        if record is None or not record.outputs:
            continue
        outputs = record.outputs
        port = "image" if "image" in outputs else next(iter(outputs))
        ref = outputs.get(port)
        if ref is None:
            continue
        project_dict["preview_hash"] = ref.node_hash
        project_dict["preview_port"] = port
        return project_dict
    return project_dict


def _project_to_response(project, conn: sqlite3.Connection) -> dict:
    payload = project.to_public_dict()
    _attach_preview(payload)
    payload["capture"] = _capture_for_project(
        conn, payload.get("source_session_ids") or []
    ).model_dump(mode="json")
    return payload


@app.get("/api/projects")
def list_projects(conn: DBDep) -> list[dict]:
    out = [_project_to_response(p, conn) for p in project_manager.list()]
    out.sort(key=lambda p: p["updated_at"], reverse=True)
    return out


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, conn: DBDep) -> dict:
    project = project_manager.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    return _project_to_response(project, conn)


class GalleryEntry(BaseModel):
    """One render in the gallery: a successful project history entry,
    surfaced as a flat row so the UI doesn't have to fan out per
    project."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_name: str
    target_common_name: str | None = None
    template_id: str
    seq: int
    label: str | None = None
    created_at: str
    preview_hash: str
    preview_port: str
    is_cover: bool = False


@app.get("/api/gallery", response_model=list[GalleryEntry])
def list_gallery(conn: DBDep) -> list[GalleryEntry]:
    """Every successful history entry across all projects, newest
    first. Each entry has the bits a card needs (preview pointer +
    target/template names) so the UI doesn't have to JOIN per render."""
    out: list[GalleryEntry] = []
    for project in project_manager.list():
        capture = _capture_for_project(conn, project.source_session_ids)
        target_common = capture.target_common_name
        for entry in project.history:
            # Opt-in: only entries the user explicitly published surface
            # in the gallery. Iterate-and-compare crumbs stay private.
            if not entry.published:
                continue
            record = job_manager.get(entry.job_id)
            if record is None or not record.outputs:
                continue
            outs = record.outputs
            port = "image" if "image" in outs else next(iter(outs))
            ref = outs.get(port)
            if ref is None:
                continue
            out.append(
                GalleryEntry(
                    project_id=project.id,
                    project_name=project.name,
                    target_common_name=target_common,
                    template_id=project.template.id,
                    seq=entry.seq,
                    label=entry.label,
                    created_at=entry.created_at,
                    preview_hash=ref.node_hash,
                    preview_port=port,
                    is_cover=(project.cover_seq == entry.seq),
                )
            )
    out.sort(key=lambda e: e.created_at, reverse=True)
    return out


@app.patch("/api/projects/{project_id}")
def patch_project(project_id: str, req: PatchProjectRequest) -> dict:
    """Apply param overrides (and optionally toggle draft_mode), submit a new
    job, and append a history entry. Returns the updated project."""
    try:
        project = project_manager.patch(
            project_id,
            overrides=req.overrides,
            draft_mode=req.draft_mode,
            label=req.label,
            force=req.force,
        )
    except ProjectNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"project {project_id} not found"
        ) from exc
    return project.to_public_dict()


class SetCoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int | None = None


@app.put("/api/projects/{project_id}/cover")
def set_project_cover(
    project_id: str, req: SetCoverRequest, conn: DBDep
) -> dict:
    """Pin the history seq used as this project's cover image. Pass
    seq=null to clear (Projects list + Gallery fall back to the auto
    pick: latest entry with outputs)."""
    try:
        project = project_manager.set_cover(project_id, req.seq)
    except ProjectNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"project {project_id} not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_to_response(project, conn)


class SetPublishedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    published: bool


@app.put("/api/projects/{project_id}/history/{seq}/published")
def set_history_published(
    project_id: str, seq: int, req: SetPublishedRequest, conn: DBDep
) -> dict:
    """Toggle a single history entry's gallery-visible state. The gallery
    feed filters to published-only; this is the user's opt-in surface."""
    try:
        project = project_manager.set_published(project_id, seq, req.published)
    except ProjectNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"project {project_id} not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_to_response(project, conn)


@app.post("/api/projects/{project_id}/revert/{seq}")
def revert_project(project_id: str, seq: int) -> dict:
    """Move the current pointer to history seq `seq`. No new job; the prior
    history entry's job_id is what the UI displays."""
    try:
        project = project_manager.revert(project_id, seq)
    except ProjectNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"project {project_id} not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return project.to_public_dict()


@app.delete("/api/projects/{project_id}")
def delete_project_endpoint(project_id: str) -> dict:
    """Remove a project and any cache entries it owns alone. Shared entries
    are left in place — purging them would invalidate other projects."""
    if project_manager.get(project_id) is None:
        raise HTTPException(
            status_code=404, detail=f"project {project_id} not found"
        )
    # Cancel any in-flight job before pulling state out from under it.
    rec = project_manager.get(project_id)
    if rec is not None:
        for entry in rec.history:
            job_manager.cancel(entry.job_id)
    evicted, freed = delete_project(
        project_id, job_manager.cache, db_path=job_manager.db_path
    )
    project_manager.forget(project_id)
    log.info(
        "project deleted: %s evicted=%d freed=%d",
        project_id, evicted, freed,
    )
    return {"evicted_count": evicted, "bytes_freed": freed}


@app.delete("/api/projects/{project_id}/cache")
def purge_project_cache_endpoint(
    project_id: str, keep_outputs: bool = False
) -> dict:
    """Evict the project's owned cache entries without deleting the
    project itself. With keep_outputs=true, terminal-output hashes
    survive (the user keeps the saved final image, loses the
    intermediates)."""
    if project_manager.get(project_id) is None:
        raise HTTPException(
            status_code=404, detail=f"project {project_id} not found"
        )
    evicted, freed = purge_project_cache(
        job_manager.cache, project_id,
        keep_outputs=keep_outputs, db_path=job_manager.db_path,
    )
    return {"evicted_count": evicted, "bytes_freed": freed}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@app.get("/api/storage")
def get_storage() -> dict:
    """System-wide storage snapshot: total cache bytes, dead bytes, and a
    per-project breakdown of owned vs shared bytes."""
    snap = system_storage(job_manager.cache, db_path=job_manager.db_path)
    return {
        "total_bytes": snap.total_bytes,
        "entry_count": snap.entry_count,
        "unreachable_bytes": snap.unreachable_bytes,
        "unreachable_count": snap.unreachable_count,
        "cache_root": snap.cache_root,
        "cache_disk": {
            "total_bytes": snap.cache_disk.total_bytes,
            "used_bytes": snap.cache_disk.used_bytes,
            "free_bytes": snap.cache_disk.free_bytes,
        },
        "per_project": [
            {
                "project_id": p.project_id,
                "name": p.name,
                "updated_at": p.updated_at,
                "owned_bytes": p.owned_bytes,
                "shared_bytes": p.shared_bytes,
                "entry_count": p.entry_count,
            }
            for p in snap.per_project
        ],
    }


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_bytes: int | None = None
    """Override the configured budget for this run. Defaults to whatever's
    in settings (or DEFAULT_CACHE_MAX_BYTES if unset)."""


@app.post("/api/storage/cleanup")
def storage_cleanup(req: CleanupRequest | None = None) -> dict:
    """Run an eviction pass against the configured (or override) budget."""
    if req and req.max_bytes is not None:
        max_bytes = req.max_bytes
    else:
        max_bytes = int(
            get_setting(
                SETTING_CACHE_MAX_BYTES,
                default_cache_max_bytes_for(job_manager.cache.root),
                db_path=job_manager.db_path,
            )
        )
    result = run_cleanup(
        job_manager.cache, max_bytes=max_bytes, db_path=job_manager.db_path
    )
    log.info(
        "storage cleanup: evicted=%d freed=%d remaining=%d over_budget=%s",
        result.evicted_count, result.bytes_freed, result.bytes_remaining,
        result.over_budget,
    )
    return {
        "evicted_count": result.evicted_count,
        "bytes_freed": result.bytes_freed,
        "bytes_remaining": result.bytes_remaining,
        "over_budget": result.over_budget,
        "max_bytes": max_bytes,
    }


@app.get("/api/settings")
def get_settings() -> dict:
    # cache_root_override is what's persisted; cache_root_active is what
    # the running process is actually using. They differ when the user
    # changed the override since the last server restart — the UI uses
    # the gap to render a 'restart required' hint.
    persisted_root = get_setting(
        SETTING_CACHE_ROOT_OVERRIDE, None, db_path=job_manager.db_path
    )
    capture_root = get_setting(
        SETTING_CAPTURE_ROOT, None, db_path=job_manager.db_path
    )
    return {
        SETTING_CACHE_MAX_BYTES: int(
            get_setting(
                SETTING_CACHE_MAX_BYTES,
                default_cache_max_bytes_for(job_manager.cache.root),
                db_path=job_manager.db_path,
            )
        ),
        SETTING_CACHE_ROOT_OVERRIDE: persisted_root,
        "cache_root_active": str(job_manager.cache.root),
        SETTING_CAPTURE_ROOT: capture_root,
    }


class PatchSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cache_max_bytes: int | None = None
    cache_root: str | None = None
    """A path on disk where the cache should live. Pass '' (empty string) to
    clear the override and fall back to the env-var / platform default. Takes
    effect on the next server restart — the running ContentCache won't move
    files from the old location."""
    capture_root: str | None = None
    """Where the user's raw captures live (Dwarf 3 SD copy, etc.). Pass ''
    to clear. Validated as: absolute, exists, readable. Effect is immediate
    — the next /api/scan call uses this path."""


@app.patch("/api/settings")
def patch_settings(req: PatchSettingsRequest) -> dict:
    if req.cache_max_bytes is not None:
        if req.cache_max_bytes < MIN_CACHE_MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail="cache_max_bytes must be at least 1 GiB",
            )
        set_setting(
            SETTING_CACHE_MAX_BYTES, req.cache_max_bytes, db_path=job_manager.db_path
        )
    if req.cache_root is not None:
        new_root = req.cache_root.strip()
        if new_root == "":
            # Empty string = clear the override (restart -> default location).
            set_setting(
                SETTING_CACHE_ROOT_OVERRIDE, None, db_path=job_manager.db_path
            )
        else:
            # Validate: path must be absolute, must exist (or be createable),
            # must be writeable. Catch the typo cases at PATCH time so the
            # next restart doesn't fail.
            from pathlib import Path as _Path
            p = _Path(new_root).expanduser()
            if not p.is_absolute():
                raise HTTPException(
                    status_code=400,
                    detail=f"cache_root must be an absolute path, got {new_root!r}",
                )
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"cache_root path can't be created: {exc}",
                ) from exc
            if not os.access(p, os.W_OK):
                raise HTTPException(
                    status_code=400,
                    detail=f"cache_root {p} exists but isn't writable",
                )
            set_setting(
                SETTING_CACHE_ROOT_OVERRIDE,
                str(p.resolve()),
                db_path=job_manager.db_path,
            )
    if req.capture_root is not None:
        new_capture = req.capture_root.strip()
        if new_capture == "":
            set_setting(
                SETTING_CAPTURE_ROOT, None, db_path=job_manager.db_path
            )
        else:
            from pathlib import Path as _Path
            cp = _Path(new_capture).expanduser()
            if not cp.is_absolute():
                raise HTTPException(
                    status_code=400,
                    detail=f"capture_root must be an absolute path, got {new_capture!r}",
                )
            # Captures are read-only from astrolab's perspective; we don't
            # mkdir on the user's behalf — the directory must already exist
            # so a typo doesn't silently create an empty folder the rescan
            # then walks for nothing.
            if not cp.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=f"capture_root {cp} does not exist or isn't a directory",
                )
            if not os.access(cp, os.R_OK):
                raise HTTPException(
                    status_code=400,
                    detail=f"capture_root {cp} isn't readable",
                )
            set_setting(
                SETTING_CAPTURE_ROOT,
                str(cp.resolve()),
                db_path=job_manager.db_path,
            )
    return get_settings()


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
                "ui_depends_on": spec.ui_depends_on,
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
def get_preview(
    node_hash: str, port: str, neutral: int = 1
) -> FileResponse:
    """Render (or return cached) thumbnail PNG for a node's output.

    The preview is cached inside the node's cache entry so subsequent loads
    are a static file read. FITS artifacts get an autostretched render;
    PNG artifacts pass through.

    `neutral=0` bypasses the per-channel rebalance (debug affordance —
    shows what Siril's linked autostretch produces, which is what you'd
    see opening the FITS in Siril directly). Default `neutral=1` runs an
    OSC-friendly per-channel stretch so pre-rgb_equal stages stop looking
    swampy green.
    """
    try:
        path = render_preview(
            job_manager.cache, node_hash, port, neutral=bool(neutral)
        )
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
# Static UI (SvelteKit static build)
# ---------------------------------------------------------------------------
# Serves the prebuilt UI from ui/build/ when present. On macOS dev we usually
# don't run `npm run build`, so the directory is missing — in that case we
# skip the mount entirely and the user keeps using `npm run dev` on :5173.
# Hosting layout on the Linux box: `npm run build` populates ui/build/, and
# astrolab-api serves the whole app on :8000 (no separate vite-dev process).

_UI_BUILD_DIR = Path(__file__).resolve().parent.parent / "ui" / "build"


if _UI_BUILD_DIR.is_dir():
    # Long-cache the hashed _app/* assets — SvelteKit's static adapter
    # fingerprints them so cache busting Just Works.
    _app_assets = _UI_BUILD_DIR / "_app"
    if _app_assets.is_dir():
        app.mount(
            "/_app",
            StaticFiles(directory=_app_assets),
            name="ui-app-assets",
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_ui(full_path: str) -> FileResponse:
        """Catch-all for the UI: serve the requested file if it exists,
        otherwise fall back to index.html (SPA-style routing).

        Registered last so it doesn't shadow any /api/* route. /api/* paths
        are handled by their respective route handlers and never reach
        here; everything else either resolves to a real static file or
        the SPA shell.
        """
        if full_path == "":
            target = _UI_BUILD_DIR / "index.html"
        else:
            candidate = (_UI_BUILD_DIR / full_path).resolve()
            # Path-traversal guard: make sure the resolved file stays under
            # the UI build dir; reject paths that try to climb out.
            if not str(candidate).startswith(str(_UI_BUILD_DIR.resolve())):
                raise HTTPException(status_code=404)
            target = candidate if candidate.is_file() else _UI_BUILD_DIR / "index.html"
        if not target.is_file():
            raise HTTPException(status_code=404)
        # No long cache on index.html — the served HTML is what changes
        # when we rebuild; assets it references are content-hashed and
        # caching them is handled by the StaticFiles mount above.
        headers = {"Cache-Control": "no-cache"} if target.name == "index.html" else None
        return FileResponse(target, headers=headers)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# Make `python -m server.api` start a dev server.
def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("server.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
