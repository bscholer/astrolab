"""FastAPI application for the astrolab control plane.

Endpoints:
- GET  /api/health                       liveness ping
- GET  /api/targets                      target list with frame counts
- GET  /api/targets/{id}                 target detail: sessions + calibration
- GET  /api/sessions/{id}                session detail: frames summary + cal
- PATCH /api/sessions/{id}               reassign a session to another target
- GET  /api/sessions/{id}/reassign_candidates  nearby targets + catalog suggestions
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
- GET  /api/system                       host telemetry (CPU/mem/disk/GPU + jobs)
- POST /api/storage/cleanup              run eviction sweep
- GET  /api/settings                     read system settings
- PATCH /api/settings                    update settings (cache_max_bytes)

Phase 2 keeps job state in memory; persistence + worker scaling come later.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sqlite3
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, model_validator

import nodes.basic  # noqa: F401  registers nodes for job execution
import server.catalog.adapters  # noqa: F401  registers ingest adapters
import server.sky as sky
from server.catalog.common_names import lookup as lookup_common_name
from server.catalog.db import open_db
from server.catalog.fits_reader import normalize_target
from server.catalog.openngc import all_entries as openngc_all_entries
from server.catalog.openngc import enrich as openngc_enrich
from server.catalog.scanner import resolve_target as resolve_target_row
from server.catalog.scanner import scan as run_scan
from server.catalog.sky_match import (
    nearby_matches,
    suggest_tolerance_deg,
    target_centroid,
    target_envelope,
)
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
    SETTING_SITE_ELEVATION_M,
    SETTING_SITE_LATITUDE,
    SETTING_SITE_LONGITUDE,
    default_cache_max_bytes_for,
    delete_project,
    get_setting,
    purge_project_cache,
    run_cleanup,
    set_setting,
    system_storage,
)
from server.system_metrics import collect_system_metrics
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


class ResolvedAs(BaseModel):
    """Catalog resolution for a target.

    Only present on responses when the source is 'position'. Name-resolved
    targets ('source' == 'name' internally) deliberately surface as
    `resolved_as = None` so the UI doesn't render a redundant caption
    next to a name the user already typed in the obvious form
    (i.e. "M 31" -> "NGC 224" is implicit and the library should read
    quietly).
    """

    model_config = ConfigDict(extra="forbid")

    canonical: str
    common_name: str | None = None
    object_type: str | None = None
    separation_arcmin: float | None = None
    source: Literal["position"]


class TargetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    common_name: str | None = None
    sky: SkyInfo | None = None
    resolved_as: ResolvedAs | None = None
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
    # User-attached free-text notes. None when no note has been saved.
    description: str | None = None


class TargetDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    common_name: str | None = None
    sky: SkyInfo | None = None
    resolved_as: ResolvedAs | None = None
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


def _resolved_as_from_row(row: sqlite3.Row) -> ResolvedAs | None:
    """Build the public ResolvedAs payload from a `targets` row.

    Returns None when the target's resolution came from its name
    (source='name') OR when there's no resolution at all. Name-resolved
    targets deliberately surface as None so the library doesn't paint a
    caption next to a name that already conveys the catalog mapping.
    """
    source = row["resolved_source"]
    if source != "position":
        # 'name' and unresolved both surface as None: we don't want
        # the UI showing a caption for the redundant case.
        return None
    canonical = row["resolved_canonical"]
    if not canonical:
        return None
    entry = openngc_enrich(canonical)
    sep = row["resolved_separation_arcmin"]
    return ResolvedAs(
        canonical=canonical,
        common_name=entry.common_name if entry is not None else None,
        object_type=entry.object_type if entry is not None else None,
        separation_arcmin=float(sep) if sep is not None else None,
        source="position",
    )


def _calibration_for_session(conn: sqlite3.Connection, session_id: int) -> list[CalibrationStatus]:
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
    # `description` is post-migration; row may predate v10 and not carry
    # the column at all. Guard with row.keys() membership so old rows
    # surface as the "no note" None default.
    description: str | None = (
        row["description"]
        if "description" in row.keys()  # noqa: SIM118
        else None
    )
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
        description=description,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/_deploy", status_code=202)
def trigger_deploy(request: Request) -> dict[str, str]:
    """Internal webhook that kicks off astrolab-deploy.service on the box.

    Auth lives at the edge: Cloudflare Access fronts this hostname and the
    /api/_deploy path is gated by a service-token-only policy, so the only
    callers that reach us are CI runs holding the service token. CF Access
    consumes the CF-Access-Client-Id/Secret headers itself and strips them
    before forwarding; what reaches origin is a `Cf-Access-Jwt-Assertion`
    header containing CF's signed JWT for the authenticated request. We
    require that header's presence as defense in depth against direct LAN
    hits to 192.168.1.254:8000 bypassing the tunnel entirely.
    (The JWT signature could be verified against
    https://benscholer.cloudflareaccess.com/cdn-cgi/access/certs, but
    presence is sufficient since CF Access wouldn't issue one without a
    valid policy match.)

    The handler shells out to `sudo systemctl start --no-block
    astrolab-deploy.service` and returns 202. The deploy unit is a
    separate cgroup, so the eventual `systemctl restart astrolab-api`
    inside the deploy script doesn't kill the in-flight request before
    the client sees the response (the response is already sent).
    """
    if not request.headers.get("Cf-Access-Jwt-Assertion"):
        raise HTTPException(403, detail="missing CF Access JWT")
    if not shutil.which("systemctl"):
        # Local dev / Mac: pretend we did the thing so end-to-end tests
        # of the route's contract pass without systemd being present.
        log.warning("/api/_deploy called without systemctl on PATH; no-op")
        return {"status": "noop", "reason": "systemctl not available"}
    try:
        subprocess.run(
            ["sudo", "-n", "systemctl", "start", "--no-block", "astrolab-deploy.service"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        log.exception("systemctl start astrolab-deploy.service failed")
        detail = f"deploy launch failed: {exc.stderr or exc.stdout}"
        raise HTTPException(500, detail=detail) from exc
    return {"status": "queued"}


@app.get("/api/targets", response_model=list[TargetSummary])
def list_targets(conn: DBDep) -> list[TargetSummary]:
    # Integration time is summed at the session level so a session
    # without exptime contributes 0 instead of NULL-poisoning the
    # whole row. Bytes are computed in a separate scalar query so the
    # frame join doesn't blow up the per-target session count.
    rows = conn.execute(
        """
        SELECT t.id, t.name,
               t.resolved_canonical, t.resolved_separation_arcmin,
               t.resolved_at, t.resolved_source,
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
        # so the UI can show '-' instead of '0s'.
        integ = float(r["integration_seconds"]) or None
        summaries.append(
            TargetSummary(
                id=r["id"],
                name=r["name"],
                common_name=common,
                sky=sky,
                resolved_as=_resolved_as_from_row(r),
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
def get_target(target_id: int, conn: DBDep) -> TargetDetail:
    target = conn.execute(
        "SELECT id, name, resolved_canonical, resolved_separation_arcmin, "
        "resolved_at, resolved_source "
        "FROM targets WHERE id = ?",
        (target_id,),
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
        resolved_as=_resolved_as_from_row(target),
        sessions=[_row_to_session_summary(conn, s, target_name=target["name"]) for s in sessions],
    )


@app.get("/api/sessions/{session_id}", response_model=SessionSummary)
def get_session(session_id: int, conn: DBDep) -> SessionSummary:
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


# ---------------------------------------------------------------------------
# Session reassign (per-session "change target" action)
# ---------------------------------------------------------------------------


class SessionPatchRequest(BaseModel):
    """Body for PATCH /api/sessions/{id}.

    Three independent operations the user may pass:
    - reassign to an existing target via `target_id`
    - reassign to a (new or reused) target via `new_target_name`
    - update freeform `description`

    Reassign uses exactly-one-of semantics across `target_id` and
    `new_target_name`. If both are unset, the request is a metadata-only
    update (description). If neither is set and description is also
    omitted, the call is a no-op; we return the current session row
    rather than 400 since "no-op succeeds" is cheaper for clients.

    `description=None` after model parse means "field omitted" if it
    wasn't in `model_fields_set`, or "explicit clear" if it was. The
    handler discriminates via `model_fields_set`.
    """

    model_config = ConfigDict(extra="forbid")

    target_id: int | None = None
    new_target_name: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _validate_reassign(self) -> SessionPatchRequest:
        # Either side of the reassign pair may be unset (then no reassign
        # happens). What's NOT allowed is both set at once: that would
        # make the operation ambiguous.
        has_id = self.target_id is not None
        has_name = bool(self.new_target_name is not None and self.new_target_name.strip())
        if has_id and has_name:
            raise ValueError("set exactly one of target_id or new_target_name, not both")
        return self


class SessionPatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionSummary
    # Target ids that were deleted as a result of a reassign (because
    # they're now empty). Empty when the patch was description-only.
    deleted_target_ids: list[int]


def _maybe_delete_empty_target(conn: sqlite3.Connection, target_id: int) -> bool:
    """Drop the target if no sessions reference it. Returns True when deleted."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE target_id = ?",
        (target_id,),
    ).fetchone()
    if row is not None and row["n"] == 0:
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        return True
    return False


@app.patch("/api/sessions/{session_id}", response_model=SessionPatchResponse)
def patch_session(session_id: int, req: SessionPatchRequest, conn: DBDep) -> SessionPatchResponse:
    """Update session metadata and / or reassign to a different target.

    Three independent fields the caller may set:
      - `target_id` or `new_target_name`: reassign (mutually exclusive).
      - `description`: free-text notes; empty string clears.

    Reassign math: the session's `frames` rows are linked by
    `session_key` (not target_id directly), so flipping
    `sessions.target_id` is all that's needed. Any source target left
    without sessions gets deleted; the response carries the deleted ids
    so the UI can drop them locally.

    For `new_target_name` the input is normalized first; a normalized
    name that matches an existing target reuses that row rather than
    creating a duplicate (the user asked for "straight up change it",
    so collisions merge instead of raising).
    """
    description_supplied = "description" in req.model_fields_set
    reassign_requested = req.target_id is not None or bool((req.new_target_name or "").strip())

    session_row = conn.execute(
        "SELECT id, target_id FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if session_row is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    source_target_id = session_row["target_id"]

    deleted: list[int] = []
    new_target_id: int = -1
    dest_name: str = ""
    with conn:
        if description_supplied:
            # Normalize empty/whitespace to NULL so the "no note" sentinel
            # is unambiguous in DB + DTO.
            if req.description is None:
                new_value: str | None = None
            else:
                stripped = req.description.strip()
                new_value = stripped if stripped else None
            conn.execute(
                "UPDATE sessions SET description = ? WHERE id = ?",
                (new_value, session_id),
            )
        if reassign_requested and req.target_id is not None:
            dest_row = conn.execute(
                "SELECT id, name FROM targets WHERE id = ?", (req.target_id,)
            ).fetchone()
            if dest_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"target {req.target_id} not found",
                )
            new_target_id = int(dest_row["id"])
            dest_name = dest_row["name"]
        elif reassign_requested:
            raw_name = req.new_target_name or ""
            normalized = normalize_target(raw_name)
            if not normalized:
                raise HTTPException(
                    status_code=400,
                    detail="new_target_name must be non-empty after normalization",
                )
            existing = conn.execute(
                "SELECT id, name FROM targets WHERE name = ?", (normalized,)
            ).fetchone()
            if existing is not None:
                new_target_id = int(existing["id"])
                dest_name = existing["name"]
            else:
                cur = conn.execute("INSERT INTO targets (name) VALUES (?)", (normalized,))
                new_target_id = int(cur.lastrowid or -1)
                dest_name = normalized

        # No-op when the user picks the session's current target, or when
        # the patch was metadata-only (description). Still return a fresh
        # response so the client gets a current snapshot.
        if reassign_requested and new_target_id != source_target_id:
            conn.execute(
                "UPDATE sessions SET target_id = ? WHERE id = ?",
                (new_target_id, session_id),
            )
            # Re-run resolution on the destination target; a newly
            # created target with no resolved_canonical yet may now pick
            # one up if its frames have position data.
            resolve_target_row(conn, new_target_id, dest_name)
            # Source target may now be empty -> drop it.
            if source_target_id is not None and _maybe_delete_empty_target(conn, source_target_id):
                deleted.append(int(source_target_id))

    # Refetch a fresh SessionSummary; the join needs the destination
    # target's name so the UI's session card updates without a full
    # library reload.
    fresh = conn.execute(
        """
        SELECT s.*, t.name AS target_name
        FROM sessions s LEFT JOIN targets t ON s.target_id = t.id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if fresh is None:  # pragma: no cover  (we just updated it)
        raise HTTPException(status_code=500, detail="session vanished mid-reassign")
    summary = _row_to_session_summary(conn, fresh, target_name=fresh["target_name"])
    return SessionPatchResponse(session=summary, deleted_target_ids=deleted)


class ReassignCandidateTarget(BaseModel):
    """Existing target near the session's centroid; the UI offers these
    as one-click "move this session to <existing target>" options."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    common_name: str | None = None
    separation_arcmin: float


class ReassignCandidateCatalog(BaseModel):
    """OpenNGC entry near the session's centroid; the UI offers these as
    "create a new target named X and move" options."""

    model_config = ConfigDict(extra="forbid")

    canonical: str
    common_name: str | None = None
    object_type: str | None = None
    separation_arcmin: float


class ReassignCandidatesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[ReassignCandidateTarget]
    catalog: list[ReassignCandidateCatalog]


def _session_centroid(
    conn: sqlite3.Connection, session_id: int
) -> tuple[float, float, float | None] | None:
    """Compute the session's centroid + envelope from its frames.

    Returns (ra_deg, dec_deg, envelope_deg or None) when at least the
    centroid is computable, else None. The envelope drives the suggestion
    tolerance; falling back to None means we use the fallback tol.
    """
    from server.catalog.sky_match import frames_to_sky

    rows = conn.execute(
        """
        SELECT f.ra AS ra, f.dec AS dec, f.fits_headers AS hdr
        FROM frames f
        JOIN sessions s ON s.session_key = f.session_key
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchall()
    # Lift the dict-rows into FrameSky records the way the scanner does.
    import json as _json

    dicts: list[dict[str, Any]] = []
    for r in rows:
        hdr: dict[str, Any] = {}
        blob = r["hdr"]
        if blob is not None:
            try:
                hdr = _json.loads(bytes(blob).decode("utf-8"))
            except (UnicodeDecodeError, _json.JSONDecodeError, TypeError):
                hdr = {}
        dicts.append(
            {
                "ra": r["ra"],
                "dec": r["dec"],
                "focallen": hdr.get("FOCALLEN"),
                "xpixsz": hdr.get("XPIXSZ"),
                "ypixsz": hdr.get("YPIXSZ"),
                "naxis1": hdr.get("NAXIS1"),
                "naxis2": hdr.get("NAXIS2"),
            }
        )
    frames = frames_to_sky(dicts)
    envelope = target_envelope(frames)
    if envelope is not None:
        ra_c, dec_c, env_deg = envelope
        return ra_c, dec_c, env_deg
    centroid = target_centroid(frames)
    if centroid is None:
        return None
    return centroid[0], centroid[1], None


@app.get(
    "/api/sessions/{session_id}/reassign_candidates",
    response_model=ReassignCandidatesResponse,
)
def get_session_reassign_candidates(session_id: int, conn: DBDep) -> ReassignCandidatesResponse:
    """Suggest reassign targets for a session.

    Returns two parallel lists:
    - `targets`: existing target rows within the suggestion tolerance of
      the session's centroid, sorted by ascending separation, capped at
      10. Excludes the session's current target.
    - `catalog`: OpenNGC entries within the same tolerance, capped at 15.

    Both lists are empty when the session has no usable centroid (no
    RA/Dec on its frames). The UI falls back to a free-text picker in
    that case.
    """
    session_row = conn.execute(
        "SELECT id, target_id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if session_row is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    current_target_id = session_row["target_id"]

    centroid_info = _session_centroid(conn, session_id)
    if centroid_info is None:
        return ReassignCandidatesResponse(targets=[], catalog=[])
    ra_c, dec_c, env_deg = centroid_info
    tol = suggest_tolerance_deg(env_deg)

    # Catalog: reuse the suggestion list the old override editor fed off.
    catalog_matches = nearby_matches(ra_c, dec_c, tol, limit=15)
    catalog_out = [
        ReassignCandidateCatalog(
            canonical=m.canonical,
            common_name=m.common_name,
            object_type=m.object_type,
            separation_arcmin=m.separation_deg * 60.0,
        )
        for m in catalog_matches
    ]

    # Existing targets: walk every target with a resolved canonical
    # position (via OpenNGC enrichment of resolved_canonical or its name)
    # and keep the ones whose catalog position lands within tolerance.
    # We don't need to compute per-target centroids here: the resolution
    # pass already mapped each target to a catalog row when possible.
    target_rows = conn.execute("SELECT id, name, resolved_canonical FROM targets").fetchall()
    from server.catalog.sky_match import angular_separation_deg

    target_out: list[ReassignCandidateTarget] = []
    for t in target_rows:
        if current_target_id is not None and t["id"] == current_target_id:
            continue
        # Prefer the persisted canonical, fall back to enriching the
        # stored name (covers targets the scanner couldn't resolve but
        # whose name still maps in OpenNGC at API-call time).
        canonical = t["resolved_canonical"]
        entry = openngc_enrich(canonical) if canonical else openngc_enrich(t["name"])
        if entry is None or entry.ra_deg is None or entry.dec_deg is None:
            continue
        sep = angular_separation_deg(ra_c, dec_c, entry.ra_deg, entry.dec_deg)
        if sep > tol:
            continue
        target_out.append(
            ReassignCandidateTarget(
                id=int(t["id"]),
                name=t["name"],
                common_name=entry.common_name,
                separation_arcmin=sep * 60.0,
            )
        )
    target_out.sort(key=lambda c: c.separation_arcmin)
    target_out = target_out[:10]
    return ReassignCandidatesResponse(targets=target_out, catalog=catalog_out)


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
        job_id,
        req.session_id,
        req.template_id,
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


def _enriched_job_dict(record, conn: sqlite3.Connection, *, include_template: bool = False) -> dict:
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
        for r in sorted(job_manager.list_jobs(), key=lambda r: r.submitted_at, reverse=True)
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
    """Return the persisted event history for a job (snapshot, not live)."""
    if job_manager.get(job_id) is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return [ev.to_dict() for ev in job_manager.get_events(job_id)]


_EVENT_POLL_SECONDS = 0.25


@app.websocket("/api/jobs/{job_id}/events")
async def stream_job_events(ws: WebSocket, job_id: str) -> None:
    """Live event stream for a job. With the worker in a separate process,
    we tail job_events by seq cursor instead of sharing an asyncio.Queue."""
    await ws.accept()
    if job_manager.get(job_id) is None:
        await ws.close(code=4404, reason=f"job {job_id} not found")
        return
    last_seq = -1
    try:
        while True:
            events = await asyncio.to_thread(job_manager.get_events, job_id, after_seq=last_seq)
            for ev in events:
                await ws.send_json(ev.to_dict())
                last_seq += 1
                if ev.type in ("job_completed", "job_failed", "job_interrupted"):
                    return
            await asyncio.sleep(_EVENT_POLL_SECONDS)
    except WebSocketDisconnect:
        pass
    finally:
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
def create_project(req: CreateProjectRequest, conn: DBDep) -> dict:
    project = project_manager.create(
        name=req.name,
        template=req.template,
        base_job=req.job,
        source_session_ids=req.source_session_ids,
    )
    return _project_to_response(project, conn)


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
    description: str | None = None
    """User-attached free-text notes. Pass an empty string to clear.
    Updating description does NOT submit a new job; it's metadata,
    not pipeline input. We use model_fields_set on the request to
    distinguish 'description was supplied' from 'omitted'."""


@app.post("/api/projects/from_session")
def create_project_from_session(req: CreateProjectFromSessionRequest, conn: DBDep) -> dict:
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
        name = row["name"] if row and row["name"] else f"session {req.session_id}"

    project = project_manager.create(
        name=name,
        template=template,
        base_job=job,
        source_session_ids=[str(req.session_id)],
    )
    return _project_to_response(project, conn)


@app.post("/api/projects/from_sessions")
def create_project_from_sessions(req: CreateProjectFromSessionsRequest, conn: DBDep) -> dict:
    """Create a Project that stacks multiple compatible catalog sessions.

    Compatibility rule: every session must share target/instrument/camera/
    filter/exptime/gain/binning. The job builder enforces this and returns
    a 400 with the offending fields named when it doesn't hold. Sessions
    are de-duplicated and order-normalized so [3,1] and [1,3] hit the same
    cache lineage.
    """
    if not req.session_ids:
        raise HTTPException(status_code=400, detail="session_ids must not be empty")
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
            name = f"{target_name} ({len(sids)} sessions)" if len(sids) > 1 else target_name
        else:
            name = f"sessions {sids}"

    project = project_manager.create(
        name=name,
        template=template,
        base_job=job,
        source_session_ids=[str(s) for s in sids],
    )
    return _project_to_response(project, conn)


class ProjectDisplay(BaseModel):
    """Catalog-resolved display info for the project's prominent header.

    Set when the project's source sessions agree on a single catalog
    target (single-session projects, or multi-session bundles where every
    session resolves to the same canonical). The UI promotes `name` to
    the page header and surfaces `canonical` as the muted sub-label.

    None when no single target can be inferred (multi-target bundles)
    OR when the target's canonical lookup turned up empty. The UI falls
    back to `project.name` in those cases.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Friendly common name, e.g. 'Triangulum Galaxy'."""
    canonical: str
    """Canonical catalog id, e.g. 'NGC 598'."""


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
            f"SELECT IFNULL(SUM(size), 0) AS bytes FROM frames WHERE session_key IN ({ph})",
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


def _canonical_group_for_target_row(
    target_id: int | None, name: str | None, resolved_canonical: str | None
) -> str | None:
    """Resolve a target row to its canonical bucket key, or None when nothing
    canonical can be inferred. Mirrors the inline logic in
    `_display_for_project`; factored out so the suggestions / swap-sessions
    paths can share it without duplicating the precedence rules.

    Precedence: prefer the scanner's auto-resolved canonical, then enrich the
    stored name through OpenNGC. No user-override column post-rip.
    """
    if not target_id:
        return None
    if resolved_canonical:
        return resolved_canonical
    if not name:
        return None
    hit = openngc_enrich(name)
    return hit.canonical if hit is not None else None


def _canonical_group_for_project(conn: sqlite3.Connection, session_ids: list[str]) -> str | None:
    """Resolve the single canonical_group for a project's source sessions.

    Returns None when the project has no sessions, the sessions resolve to
    more than one canonical bucket, or any target's canonical can't be
    resolved at all. The same-target gate on `swap_sessions` reuses this so
    the rule matches what `_display_for_project` shows in the header.
    """
    if not session_ids:
        return None
    try:
        ids = [int(s) for s in session_ids]
    except (ValueError, TypeError):
        return None
    if not ids:
        return None
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT t.id AS target_id, t.name AS name,
               t.resolved_canonical AS resolved_canonical
        FROM sessions s
        LEFT JOIN targets t ON t.id = s.target_id
        WHERE s.id IN ({placeholders})
          AND t.id IS NOT NULL
        """,  # noqa: S608  (placeholders are ints)
        ids,
    ).fetchall()
    if not rows:
        return None
    canonicals: set[str] = set()
    for r in rows:
        c = _canonical_group_for_target_row(r["target_id"], r["name"], r["resolved_canonical"])
        if c is None:
            return None
        canonicals.add(c)
    if len(canonicals) != 1:
        return None
    return next(iter(canonicals))


def _canonical_group_for_session(conn: sqlite3.Connection, session_id: int) -> str | None:
    """Resolve one session's canonical_group via its target row. Returns
    None if the session has no target or the target's canonical can't be
    inferred."""
    row = conn.execute(
        """
        SELECT t.id AS target_id, t.name AS name,
               t.resolved_canonical AS resolved_canonical
        FROM sessions s
        LEFT JOIN targets t ON t.id = s.target_id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return _canonical_group_for_target_row(row["target_id"], row["name"], row["resolved_canonical"])


def _display_for_project(conn: sqlite3.Connection, session_ids: list[str]) -> ProjectDisplay | None:
    """Resolve a single (common_name, canonical) pair for the project.

    Rule:
      - Single target across all source sessions: try to coalesce its
        resolved canonical (override -> auto -> name lookup); if that
        yields an OpenNGC entry with a common_name, return it.
      - Multiple distinct targets: bail with None; the UI keeps the
        user's own project name as the header.

    Returning None is the "no friendly display" signal; the UI renders
    `project.name` in that case so we never invent a header.
    """
    if not session_ids:
        return None
    try:
        ids = [int(s) for s in session_ids]
    except (ValueError, TypeError):
        return None
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT t.id AS target_id, t.name AS name,
               t.resolved_canonical AS resolved_canonical
        FROM sessions s
        LEFT JOIN targets t ON t.id = s.target_id
        WHERE s.id IN ({placeholders})
          AND t.id IS NOT NULL
        """,  # noqa: S608  (placeholders are ints)
        ids,
    ).fetchall()
    if not rows:
        return None

    # Resolve every target row to its canonical bucket; if they all agree,
    # we have a single-target project. Two sessions of the same target
    # share a row here (DISTINCT on target id), so the typical single-
    # target project is exactly one row.
    canonicals: set[str] = set()
    sample: sqlite3.Row | None = None
    for r in rows:
        # Canonical bucket lookup, inlined: prefer the scanner's auto-
        # resolved canonical, else try enrich(name). No user-override
        # column post-rip; this is purely metadata.
        c = r["resolved_canonical"]
        if not c:
            hit = openngc_enrich(r["name"])
            c = hit.canonical if hit is not None else None
        if c is None:
            # An unresolved target sinks the whole project to "no
            # display"; we don't want to silently drop one target's
            # contribution to a multi-target bundle.
            return None
        canonicals.add(c)
        sample = r
    if len(canonicals) != 1 or sample is None:
        return None
    canonical = next(iter(canonicals))
    entry = openngc_enrich(canonical)
    if entry is None:
        # The bucket key isn't in OpenNGC (e.g. a freeform user-pinned
        # value). Fall back to the curated common-names table for a
        # friendly label; if even that's empty, surface None so the UI
        # keeps the project name visible.
        common = lookup_common_name(canonical)
        if not common:
            return None
        return ProjectDisplay(name=common, canonical=canonical)
    # OpenNGC ships a `Common names` column but it's sparsely populated;
    # most NGC/IC rows have nothing useful there. Fall back to the curated
    # common_names.json (catalogs/data/common_names.json) which fills in
    # the popular names OpenNGC doesn't carry (Wizard Nebula, Heart
    # Nebula, etc.). Same precedence as `_resolve_target_meta`.
    common = entry.common_name or lookup_common_name(entry.canonical)
    if not common:
        # No friendly name at all -> show no display block so the UI
        # falls back to the user's stored target name.
        return None
    return ProjectDisplay(name=common, canonical=entry.canonical)


class SuggestedAdditions(BaseModel):
    """Sessions on the project's canonical_group that aren't yet bound
    to the project. Surfaced inline on the project-detail response so
    the "+ Add N more sessions?" banner renders on first paint without
    a second roundtrip.

    Empty lists when the project is multi-target / unresolved / there
    are no orphan sessions. `suggestions_token` is a stable hash of the
    sorted session_ids so the UI can scope a dismissal to the current
    set: capture a new session and the token changes, the banner
    reappears."""

    model_config = ConfigDict(extra="forbid")

    session_ids: list[int]
    session_count: int
    frame_count: int
    integration_seconds: float
    suggestions_token: str


def _suggestions_for_project(
    conn: sqlite3.Connection,
    canonical_group: str | None,
    existing_session_ids: list[str],
) -> SuggestedAdditions:
    """Find sessions on the same canonical_group that aren't already in
    the project. Cheap aggregate: for typical libraries we're talking
    tens of sessions and the join is a small fan-out.

    Returns an empty payload when canonical_group is None (multi-target
    project) or no orphans match. We mirror the same precedence rules
    as `_canonical_group_for_project` to keep "session N belongs to
    canonical X" consistent across both directions of the lookup.
    """
    if not canonical_group:
        return SuggestedAdditions(
            session_ids=[],
            session_count=0,
            frame_count=0,
            integration_seconds=0.0,
            suggestions_token="",
        )
    existing: set[int] = set()
    for sid in existing_session_ids:
        try:
            existing.add(int(sid))
        except (ValueError, TypeError):
            continue

    # Reference session: any of the project's existing sessions defines
    # the COMPAT_FIELDS bundle (instrument, camera, filter, exptime,
    # gain, binning) that PATCH would later enforce. We suggest only
    # orphans that match all of them, so the user never sees a "+ Add
    # 3 sessions" banner that would 400 with "exptime: 15.0, 30.0".
    compat_reference: sqlite3.Row | None = None
    if existing:
        placeholders = ",".join("?" for _ in existing)
        compat_reference = conn.execute(
            f"""
            SELECT instrument, camera, filter, exptime, gain, binning
            FROM sessions WHERE id IN ({placeholders})
            LIMIT 1
            """,  # noqa: S608  (ids are ints)
            list(existing),
        ).fetchone()

    # Pull every session whose target row resolves to the same canonical
    # bucket. We compute the bucket per-target in Python because the
    # rule (`resolved_canonical` else `openngc_enrich(name)`) doesn't
    # express cleanly in SQL. Cheap enough at typical library sizes.
    rows = conn.execute(
        """
        SELECT s.id AS session_id, s.instrument, s.camera, s.filter,
               s.exptime, s.gain, s.binning, s.frame_count, s.failed_count,
               t.id AS target_id, t.name AS target_name,
               t.resolved_canonical AS resolved_canonical
        FROM sessions s
        LEFT JOIN targets t ON t.id = s.target_id
        WHERE t.id IS NOT NULL
        """,
    ).fetchall()
    out_ids: list[int] = []
    frames = 0
    integration = 0.0
    for r in rows:
        sid = int(r["session_id"])
        if sid in existing:
            continue
        c = _canonical_group_for_target_row(
            r["target_id"], r["target_name"], r["resolved_canonical"]
        )
        if c != canonical_group:
            continue
        # Drop anything that wouldn't survive the PATCH endpoint's
        # _assert_compatible gate. The COMPAT_FIELDS list lives in
        # job_builder; we don't import it here to keep this helper
        # cheap, just enumerate the fields inline (target_id is
        # already covered by the canonical-group filter above).
        if compat_reference is not None:
            mismatch = False
            for field in ("instrument", "camera", "filter", "exptime", "gain", "binning"):
                if r[field] != compat_reference[field]:
                    mismatch = True
                    break
            if mismatch:
                continue
        out_ids.append(sid)
        usable = max(0, (r["frame_count"] or 0) - (r["failed_count"] or 0))
        frames += r["frame_count"] or 0
        if r["exptime"] is not None and usable > 0:
            integration += float(r["exptime"]) * usable
    out_ids.sort()
    if not out_ids:
        return SuggestedAdditions(
            session_ids=[],
            session_count=0,
            frame_count=0,
            integration_seconds=0.0,
            suggestions_token="",
        )
    # Stable hash over the sorted id list: a new captured session changes
    # the token, banner reappears even if the user previously dismissed.
    import hashlib as _hashlib

    token = _hashlib.sha1(",".join(str(i) for i in out_ids).encode("utf-8")).hexdigest()[:16]
    return SuggestedAdditions(
        session_ids=out_ids,
        session_count=len(out_ids),
        frame_count=frames,
        integration_seconds=integration,
        suggestions_token=token,
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
    session_ids = payload.get("source_session_ids") or []
    payload["capture"] = _capture_for_project(conn, session_ids).model_dump(mode="json")
    display = _display_for_project(conn, session_ids)
    payload["display"] = display.model_dump(mode="json") if display else None
    # Suggestions are sourced from the project's canonical_group; null when
    # the project is multi-target / unresolved or has no orphan sessions.
    # We surface the empty payload as None on the DTO so the UI's "render
    # the banner" check is a single null guard.
    canonical_group = _canonical_group_for_project(conn, session_ids)
    suggestions = _suggestions_for_project(conn, canonical_group, session_ids)
    payload["suggested_additions"] = (
        suggestions.model_dump(mode="json") if suggestions.session_ids else None
    )
    # Surface latest available template version so the UI can show / enable
    # the "Upgrade template" button only when there's something to move to.
    try:
        latest = load_template(project.template.id)
        payload["latest_template_version"] = latest.version
    except TemplateNotFound:
        payload["latest_template_version"] = project.template.version
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
def patch_project(project_id: str, req: PatchProjectRequest, conn: DBDep) -> dict:
    """Apply param overrides (and optionally toggle draft_mode), submit a new
    job, and append a history entry. Returns the updated project.

    Description is metadata: it's persisted without submitting a new
    job. We honor "description supplied" vs "omitted" via the request's
    `model_fields_set` so descriptionless PATCHes are still idempotent.

    A description-only PATCH (no overrides, no draft_mode toggle) skips
    the override-merge / job-submit path entirely so the autosave-on-blur
    UX doesn't kick the pipeline.
    """
    description_supplied = "description" in req.model_fields_set
    overrides_supplied = "overrides" in req.model_fields_set
    draft_supplied = "draft_mode" in req.model_fields_set
    force_supplied = "force" in req.model_fields_set and req.force

    try:
        # Description-only patch: skip overrides/job submission entirely.
        # Without this, autosave-on-blur would tear down the in-flight
        # job and queue a redundant new one for a metadata edit.
        if description_supplied and not (overrides_supplied or draft_supplied or force_supplied):
            project = project_manager.set_description(project_id, req.description)
        else:
            project = project_manager.patch(
                project_id,
                overrides=req.overrides,
                draft_mode=req.draft_mode,
                label=req.label,
                force=req.force,
            )
            if description_supplied:
                # Combined edit: persist the note alongside the override.
                project = project_manager.set_description(project_id, req.description)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found") from exc
    return _project_to_response(project, conn)


class SetCoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int | None = None


@app.put("/api/projects/{project_id}/cover")
def set_project_cover(project_id: str, req: SetCoverRequest, conn: DBDep) -> dict:
    """Pin the history seq used as this project's cover image. Pass
    seq=null to clear (Projects list + Gallery fall back to the auto
    pick: latest entry with outputs)."""
    try:
        project = project_manager.set_cover(project_id, req.seq)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_to_response(project, conn)


class SetPublishedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    published: bool


@app.put("/api/projects/{project_id}/history/{seq}/published")
def set_history_published(project_id: str, seq: int, req: SetPublishedRequest, conn: DBDep) -> dict:
    """Toggle a single history entry's gallery-visible state. The gallery
    feed filters to published-only; this is the user's opt-in surface."""
    try:
        project = project_manager.set_published(project_id, seq, req.published)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_to_response(project, conn)


@app.post("/api/projects/{project_id}/upgrade_template")
def upgrade_project_template(project_id: str, conn: DBDep) -> dict:
    """Move the project to the latest version of its current template.

    The on-disk template YAML may have advanced past what's frozen on the
    project row; this endpoint reloads the YAML, validates the version
    moves forward, runs `migrate_param_overrides` to carry user overrides
    forward where possible, and submits a fresh render against the new
    template.

    Per-node cache keys do not include `template_version`, so nodes whose
    identity (id, kind, version, params, input edges) is unchanged will
    cache-hit on the next render. Only inserted, removed, or rewired
    nodes recompute.

    Returns the updated project plus a `dropped_overrides` field listing
    any overrides that didn't survive the migration (removed node /
    removed param).
    """
    project = project_manager.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    try:
        latest = load_template(project.template.id)
    except TemplateNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=f"template {project.template.id!r} not found on disk",
        ) from exc
    if latest.version <= project.template.version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"project is already on the latest template version "
                f"(v{project.template.version}); on-disk template is "
                f"v{latest.version}"
            ),
        )
    try:
        updated, new_job_id, dropped = project_manager.upgrade_template(
            project_id, new_template=latest
        )
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info(
        "project template upgraded: %s v%d job=%s dropped=%d",
        project_id,
        updated.template.version,
        new_job_id,
        len(dropped),
    )
    response = _project_to_response(updated, conn)
    response["dropped_overrides"] = dropped
    response["new_job_id"] = new_job_id
    return response


@app.post("/api/projects/{project_id}/revert/{seq}")
def revert_project(project_id: str, seq: int, conn: DBDep) -> dict:
    """Move the current pointer to history seq `seq`. No new job; the prior
    history entry's job_id is what the UI displays."""
    try:
        project = project_manager.revert(project_id, seq)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_to_response(project, conn)


@app.delete("/api/projects/{project_id}")
def delete_project_endpoint(project_id: str) -> dict:
    """Remove a project and any cache entries it owns alone. Shared entries
    are left in place — purging them would invalidate other projects."""
    if project_manager.get(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    # Cancel any in-flight job before pulling state out from under it.
    rec = project_manager.get(project_id)
    if rec is not None:
        for entry in rec.history:
            job_manager.cancel(entry.job_id)
    evicted, freed = delete_project(project_id, job_manager.cache, db_path=job_manager.db_path)
    project_manager.forget(project_id)
    log.info(
        "project deleted: %s evicted=%d freed=%d",
        project_id,
        evicted,
        freed,
    )
    return {"evicted_count": evicted, "bytes_freed": freed}


@app.delete("/api/projects/{project_id}/cache")
def purge_project_cache_endpoint(project_id: str, keep_outputs: bool = False) -> dict:
    """Evict the project's owned cache entries without deleting the
    project itself. With keep_outputs=true, terminal-output hashes
    survive (the user keeps the saved final image, loses the
    intermediates)."""
    if project_manager.get(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    evicted, freed = purge_project_cache(
        job_manager.cache,
        project_id,
        keep_outputs=keep_outputs,
        db_path=job_manager.db_path,
    )
    return {"evicted_count": evicted, "bytes_freed": freed}


@app.get("/api/projects/{project_id}/cache")
def project_cache_dry_run(project_id: str, keep_outputs: bool = False) -> dict:
    """Preview the eviction the matching DELETE would perform, without
    actually evicting. Used by the Manage Sessions modal's live diff so
    the user sees `~X GB will be evicted` while toggling sessions.

    Reuses the same reachability scan as the real purge path; the only
    difference is we tally bytes instead of calling cache.evict().
    """
    if project_manager.get(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    from server.storage import (
        _conn as _storage_conn,
    )
    from server.storage import (
        _terminal_output_hashes,
        build_reachability,
    )

    with _storage_conn(job_manager.db_path) as conn:
        entries, _ = build_reachability(conn, job_manager.cache)
        keep_hashes = _terminal_output_hashes(conn, project_id) if keep_outputs else set()
    evicted = 0
    bytes_to_free = 0
    for h, e in entries.items():
        if e.owners != {project_id}:
            continue
        if h in keep_hashes:
            continue
        evicted += 1
        bytes_to_free += e.bytes
    return {"evicted_count": evicted, "bytes_to_free": bytes_to_free}


@app.get("/api/projects/{project_id}/suggestions")
def get_project_suggestions(project_id: str, conn: DBDep) -> dict:
    """Return sessions on the project's canonical_group that aren't yet
    bound to it. Empty when the project is multi-target / unresolved or
    has no orphan sessions.

    Mirrors the `suggested_additions` block carried on the main project
    DTO; exposed separately so the UI can re-fetch after a swap without
    a full project reload.
    """
    project = project_manager.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    canonical_group = _canonical_group_for_project(conn, project.source_session_ids)
    return _suggestions_for_project(conn, canonical_group, project.source_session_ids).model_dump(
        mode="json"
    )


class PatchProjectSessionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_ids: list[int]
    """Full new id list, replace semantics. Empty is rejected with 400."""
    auto_render: bool = True
    """Kick off a render right after the swap. Set False to defer; the
    project's history pointer still advances so the swap is visible
    immediately, the user just doesn't pay for a pipeline run yet."""


@app.patch("/api/projects/{project_id}/sessions")
def patch_project_sessions(project_id: str, req: PatchProjectSessionsRequest, conn: DBDep) -> dict:
    """Replace the project's source-session set.

    Gates in order: same-target (every session shares the project's
    canonical_group), calibration-compat (build_from_sessions enforces
    this), non-empty.

    On success: build a fresh Job pinned to the project's current
    template_id+template_version (no silent template upgrade, that's
    issue #61's slice), evict the project's owned cache, append a
    `swap_sessions` history entry with the new id list + frame count +
    integration time, optionally kick a render. Response carries the
    evicted bytes/count so the UI can confirm what happened.
    """
    project = project_manager.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    if not req.session_ids:
        # A project with zero sessions is nonsense; bail out clearly
        # rather than building an empty Job that would crash deep in
        # the pipeline.
        raise HTTPException(status_code=400, detail="session_ids must not be empty")

    new_ids = sorted(set(req.session_ids))
    project_canonical = _canonical_group_for_project(conn, project.source_session_ids)
    # Same-target gate: every proposed session must resolve to the same
    # canonical_group as the project. Mismatches surface with a precise
    # message naming the offending session + its canonical so the UI can
    # render a useful diagnostic.
    if project_canonical is not None:
        for sid in new_ids:
            cand_row = conn.execute(
                "SELECT s.id, t.name AS target_name FROM sessions s "
                "LEFT JOIN targets t ON t.id = s.target_id WHERE s.id = ?",
                (sid,),
            ).fetchone()
            if cand_row is None:
                raise HTTPException(status_code=404, detail=f"session {sid} not found")
            cand_canonical = _canonical_group_for_session(conn, sid)
            if cand_canonical != project_canonical:
                target_name = cand_row["target_name"] or "unknown"
                cand_label = cand_canonical or "unresolved"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"session {sid} is on target {target_name} "
                        f"(canonical {cand_label}) but this project is on "
                        f"canonical {project_canonical}"
                    ),
                )

    # Pin template id+version: a swap must NOT silently upgrade the
    # template even if a newer version is on disk. The granularity-rip
    # work in #61 will introduce explicit template upgrades.
    template = project.template
    calibration = project.base_job.calibration

    try:
        new_base_job = build_from_sessions(conn, new_ids, template, calibration)
    except (CalibrationMissing, TooFewFrames, IncompatibleSessions) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobBuildError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Capture the eviction stats BEFORE the swap so we can report what
    # the cache nuke freed; purge then runs against the current owner
    # set (the project's prior session bundle still owns the entries
    # at this point).
    from server.storage import (
        _conn as _storage_conn,
    )
    from server.storage import (
        _terminal_output_hashes,
        build_reachability,
    )

    with _storage_conn(job_manager.db_path) as scan:
        entries, _ = build_reachability(scan, job_manager.cache)
        keep_hashes = _terminal_output_hashes(scan, project_id)
    pre_evict_count = 0
    pre_evict_bytes = 0
    for h, e in entries.items():
        if e.owners != {project_id}:
            continue
        if h in keep_hashes:
            continue
        pre_evict_count += 1
        pre_evict_bytes += e.bytes

    # Aggregate frame count + integration time for the new bundle so
    # the swap_sessions history snapshot carries everything a future
    # revert / audit might want without re-querying the catalog.
    ph = ",".join("?" for _ in new_ids)
    agg_row = conn.execute(
        f"""
        SELECT
          SUM(frame_count) AS frames,
          SUM(failed_count) AS failed,
          MIN(started_at) AS started_at,
          MAX(ended_at) AS ended_at
        FROM sessions WHERE id IN ({ph})
        """,
        new_ids,
    ).fetchone()
    integration_total = 0.0
    int_rows = conn.execute(
        f"""
        SELECT exptime, frame_count, failed_count
        FROM sessions WHERE id IN ({ph})
        """,
        new_ids,
    ).fetchall()
    for r in int_rows:
        usable = max(0, (r["frame_count"] or 0) - (r["failed_count"] or 0))
        if r["exptime"] is not None and usable > 0:
            integration_total += float(r["exptime"]) * usable

    snapshot = {
        "session_ids": new_ids,
        "frame_count": int(agg_row["frames"] or 0),
        "failed_count": int(agg_row["failed"] or 0),
        "integration_seconds": integration_total,
    }

    try:
        project, new_job_id = project_manager.swap_sessions(
            project_id,
            new_base_job=new_base_job,
            new_session_ids=[str(s) for s in new_ids],
            snapshot=snapshot,
            auto_render=req.auto_render,
        )
    except ProjectNotFound as exc:  # pragma: no cover - guarded above
        raise HTTPException(status_code=404, detail=f"project {project_id} not found") from exc

    # Evict the project's now-unreachable cache. The swap created a new
    # Job, so the prior history's outputs are still reachable from the
    # earlier `kind='edit'` entries, so purge_project_cache scopes by
    # current ownership, so only entries that are now owned solely by
    # the prior session set get freed. That's the right behavior: we
    # don't want to nuke an output the user might want to revert back
    # to. (Granularity work in #61 will revisit this.)
    evicted, freed = purge_project_cache(
        job_manager.cache,
        project_id,
        keep_outputs=False,
        db_path=job_manager.db_path,
    )
    log.info(
        "project sessions swapped: %s sessions=%s evicted=%d freed=%d job=%s",
        project_id,
        new_ids,
        evicted,
        freed,
        new_job_id,
    )

    response = _project_to_response(project, conn)
    response["evicted_count"] = evicted
    response["evicted_bytes"] = freed
    response["new_job_id"] = new_job_id
    return response


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


@app.get("/api/system")
def get_system() -> dict:
    """Host telemetry for the dashboard: CPU/mem/disk/GPU and job stats."""
    return collect_system_metrics(job_manager)


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
    result = run_cleanup(job_manager.cache, max_bytes=max_bytes, db_path=job_manager.db_path)
    log.info(
        "storage cleanup: evicted=%d freed=%d remaining=%d over_budget=%s",
        result.evicted_count,
        result.bytes_freed,
        result.bytes_remaining,
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
    persisted_root = get_setting(SETTING_CACHE_ROOT_OVERRIDE, None, db_path=job_manager.db_path)
    capture_root = get_setting(SETTING_CAPTURE_ROOT, None, db_path=job_manager.db_path)
    site_lat = get_setting(SETTING_SITE_LATITUDE, None, db_path=job_manager.db_path)
    site_lon = get_setting(SETTING_SITE_LONGITUDE, None, db_path=job_manager.db_path)
    site_elev = get_setting(SETTING_SITE_ELEVATION_M, None, db_path=job_manager.db_path)
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
        SETTING_SITE_LATITUDE: (float(site_lat) if site_lat is not None else None),
        SETTING_SITE_LONGITUDE: (float(site_lon) if site_lon is not None else None),
        SETTING_SITE_ELEVATION_M: (float(site_elev) if site_elev is not None else None),
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
    site_latitude: float | None = None
    """Observer latitude in decimal degrees, north positive. Range
    -90..90. Stored alongside longitude/elevation; the Tonight planner
    needs all three before it will compute alt/az."""
    site_longitude: float | None = None
    """Observer longitude in decimal degrees, east positive. Range
    -180..180."""
    site_elevation_m: float | None = None
    """Observer elevation in meters above sea level. Used for refraction
    correction in alt/az; non-negative. We don't enforce a hard upper
    bound, the highest plausible amateur site is Mauna Kea-ish at 4200m
    so anything past 9000m almost certainly indicates a unit confusion
    and we reject it."""


@app.patch("/api/settings")
def patch_settings(req: PatchSettingsRequest) -> dict:
    if req.cache_max_bytes is not None:
        if req.cache_max_bytes < MIN_CACHE_MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail="cache_max_bytes must be at least 1 GiB",
            )
        set_setting(SETTING_CACHE_MAX_BYTES, req.cache_max_bytes, db_path=job_manager.db_path)
    if req.cache_root is not None:
        new_root = req.cache_root.strip()
        if new_root == "":
            # Empty string = clear the override (restart -> default location).
            set_setting(SETTING_CACHE_ROOT_OVERRIDE, None, db_path=job_manager.db_path)
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
            set_setting(SETTING_CAPTURE_ROOT, None, db_path=job_manager.db_path)
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
    if req.site_latitude is not None:
        if not -90.0 <= req.site_latitude <= 90.0:
            raise HTTPException(
                status_code=400,
                detail=f"site_latitude must be in [-90, 90], got {req.site_latitude}",
            )
        set_setting(
            SETTING_SITE_LATITUDE,
            float(req.site_latitude),
            db_path=job_manager.db_path,
        )
    if req.site_longitude is not None:
        if not -180.0 <= req.site_longitude <= 180.0:
            raise HTTPException(
                status_code=400,
                detail=f"site_longitude must be in [-180, 180], got {req.site_longitude}",
            )
        set_setting(
            SETTING_SITE_LONGITUDE,
            float(req.site_longitude),
            db_path=job_manager.db_path,
        )
    if req.site_elevation_m is not None:
        # Below sea level is fine (Dead Sea, etc.); past 9000m is almost
        # certainly a unit-confusion bug.
        if not -500.0 <= req.site_elevation_m <= 9000.0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"site_elevation_m must be in [-500, 9000] meters, got {req.site_elevation_m}"
                ),
            )
        set_setting(
            SETTING_SITE_ELEVATION_M,
            float(req.site_elevation_m),
            db_path=job_manager.db_path,
        )
    return get_settings()


# ---------------------------------------------------------------------------
# Tonight planner
# ---------------------------------------------------------------------------


class TonightEntry(BaseModel):
    """One row in the Tonight table.

    Sky positions are J2000/ICRS-aligned; alt/az are evaluated at the
    `at` query timestamp. `transit_local_iso` is the target's upper
    culmination during the night window in the site's wall-clock time
    (we encode the offset implied by the longitude so the UI doesn't
    have to redo the timezone math). `null` transit means the target
    doesn't culminate inside dusk-to-dawn at this site, in which case
    the UI just shows alt_now and lets the user decide."""

    model_config = ConfigDict(extra="forbid")

    name: str
    common_name: str | None = None
    object_type: str | None = None
    constellation: str | None = None
    ra_deg: float
    dec_deg: float
    magnitude: float | None = None
    alt_now_deg: float
    az_now_deg: float
    transit_utc: str | None = None
    """ISO 8601 UTC instant of the target's upper culmination during the
    night window, or null when it doesn't transit between dusk and dawn."""
    hours_above_min_alt: float
    session_count: int = 0
    """How many times the user has captured this target. Joined from the
    targets table by canonical name."""
    last_session_at: str | None = None
    alt_curve_deg: list[float] | None = None
    """Altitude samples in degrees, evenly spaced from `dusk_utc` to
    `dawn_utc` at `alt_curve_step_min` cadence (top-level field). Null
    when the night window is degenerate (polar day with no usable
    twilight). The UI renders this as a per-row sparkline."""


class TonightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at_utc: str
    """The query timestamp echoed back, normalized to UTC ISO 8601."""
    site_latitude: float
    site_longitude: float
    site_elevation_m: float
    min_alt_deg: float
    max_magnitude: float
    dusk_utc: str | None
    """Astronomical (or nautical / civil fallback) twilight start, UTC.
    Null when the site is in 24h daylight (high-latitude polar summer)."""
    dawn_utc: str | None
    alt_curve_step_min: int
    """Spacing in minutes between consecutive samples in each entry's
    `alt_curve_deg` array. Constant within a response so the UI can
    reconstruct the time axis as dusk_utc + i * step."""
    entries: list[TonightEntry]


@app.get("/api/tonight", response_model=TonightResponse)
def get_tonight(
    conn: DBDep,
    at: str | None = None,
    min_alt: float = 20.0,
    max_mag: float = 12.0,
) -> TonightResponse:
    """List visible deep-sky targets for the configured site at time `at`.

    Site coords come from the settings KV; missing any of latitude /
    longitude / elevation is a 400 with a clear message rather than a
    silent fallback. We pick that strictness on purpose: silently
    defaulting to (0,0,0) would mean the user sees a list that's wrong
    by tens of degrees and has no idea why.

    `at` defaults to now (UTC). Targets are filtered to those currently
    above `min_alt` degrees and brighter than `max_mag` V-band, then
    sorted by descending altitude so the highest, easiest-to-frame
    targets surface first.
    """
    site_lat = get_setting(SETTING_SITE_LATITUDE, None, db_path=job_manager.db_path)
    site_lon = get_setting(SETTING_SITE_LONGITUDE, None, db_path=job_manager.db_path)
    site_elev = get_setting(SETTING_SITE_ELEVATION_M, None, db_path=job_manager.db_path)
    if site_lat is None or site_lon is None or site_elev is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "site location is not configured. Set site_latitude, "
                "site_longitude, and site_elevation_m on the Settings page "
                "before using the Tonight planner."
            ),
        )

    if at is None:
        at_utc = datetime.now(UTC)
    else:
        try:
            parsed = datetime.fromisoformat(at)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"at must be ISO 8601, got {at!r}",
            ) from exc
        at_utc = parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    loc = sky.observer_location(float(site_lat), float(site_lon), float(site_elev))
    window = sky.twilight_window(loc, at_utc)
    dusk_utc, dawn_utc = window if window is not None else (None, None)

    # Pull every target the user has captured, keyed by the OpenNGC
    # canonical id of the row it resolves to. This lets us join across
    # naming conventions: a targets-table row stored as "M 31" lights
    # up the catalog entry whose canonical is "NGC 224", because the
    # OpenNGC alias index maps both to the same row. Targets that don't
    # resolve to any catalog row are stored under their literal name as
    # a fallback (e.g. user-named regions OpenNGC doesn't cover).
    captured: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT t.name AS name,
               t.resolved_canonical AS resolved_canonical,
               COUNT(s.id) AS session_count,
               MAX(s.started_at) AS last_session_at
        FROM targets t
        LEFT JOIN sessions s ON s.target_id = t.id
        GROUP BY t.id, t.name
        """
    ).fetchall():
        if row["session_count"] is None or row["session_count"] == 0:
            continue
        # Coalesce: scanner's auto canonical (may be set by either the
        # name path or the position path) -> fall back to enrich(name)
        # for callers that scanned before the resolve pass ever ran.
        # Whatever lands here is the join key for the captured-overlay
        # merge below.
        canonical: str | None = row["resolved_canonical"]
        if not canonical:
            catalog_hit = openngc_enrich(row["name"])
            canonical = catalog_hit.canonical if catalog_hit is not None else None
        key = canonical.strip().lower() if canonical else (row["name"] or "").strip().lower()
        captured[key] = {
            "session_count": row["session_count"] or 0,
            "last_session_at": row["last_session_at"],
        }

    # Pre-filter the catalog to entries with usable RA/Dec/mag, then
    # batch the alt/az transform. astropy's vectorized AltAz call is
    # ~100x faster than per-entry calls on the full ~14k OpenNGC list.
    catalog_entries = openngc_all_entries()
    candidates = []
    for entry in catalog_entries:
        if entry.ra_deg is None or entry.dec_deg is None:
            continue
        # Treat unknown magnitude as "fail the cap" so we don't surface
        # rows we can't tell the user how bright they are.
        if entry.magnitude is None or entry.magnitude > max_mag:
            continue
        candidates.append(entry)

    if not candidates:
        return TonightResponse(
            at_utc=at_utc.isoformat(),
            site_latitude=float(site_lat),
            site_longitude=float(site_lon),
            site_elevation_m=float(site_elev),
            min_alt_deg=float(min_alt),
            max_magnitude=float(max_mag),
            dusk_utc=dusk_utc.isoformat() if dusk_utc else None,
            dawn_utc=dawn_utc.isoformat() if dawn_utc else None,
            alt_curve_step_min=sky.SPARKLINE_STEP_MIN,
            entries=[],
        )

    ra_arr = np.asarray([e.ra_deg for e in candidates], dtype=float)
    dec_arr = np.asarray([e.dec_deg for e in candidates], dtype=float)
    alts, azs = sky.alt_az_batch(ra_arr, dec_arr, loc, at_utc)

    # Drop targets below the alt cutoff before the (more expensive) night-
    # window math. At a typical mid-latitude site this halves the
    # candidate set.
    visible_mask = alts >= min_alt
    visible_indices = np.where(visible_mask)[0]
    visible_ra = ra_arr[visible_mask]
    visible_dec = dec_arr[visible_mask]

    transits: list[datetime | None] = [None] * len(visible_indices)
    hours_arr = np.zeros(len(visible_indices))
    alt_curves = np.zeros((len(visible_indices), 0))
    if dusk_utc is not None and dawn_utc is not None and len(visible_indices) > 0:
        transits, hours_arr, alt_curves = sky.night_transits_and_hours(
            visible_ra, visible_dec, loc, dusk_utc, dawn_utc, min_alt
        )

    out: list[TonightEntry] = []
    for k, idx in enumerate(visible_indices):
        entry = candidates[int(idx)]
        cap = captured.get(entry.canonical.strip().lower())
        transit = transits[k]
        transit_iso = transit.isoformat() if transit is not None else None
        # Sparkline payload: round to one decimal so the JSON stays small;
        # the UI is rendering this into ~32 vertical pixels and can't see
        # tighter precision. Drop the curve entirely when the night window
        # was degenerate (zero columns) so the UI just hides the column.
        curve = [round(float(v), 1) for v in alt_curves[k]] if alt_curves.shape[1] > 0 else None
        out.append(
            TonightEntry(
                name=entry.canonical,
                common_name=entry.common_name,
                object_type=entry.object_type,
                constellation=entry.constellation,
                ra_deg=float(entry.ra_deg),
                dec_deg=float(entry.dec_deg),
                magnitude=entry.magnitude,
                alt_now_deg=float(alts[idx]),
                az_now_deg=float(azs[idx]),
                transit_utc=transit_iso,
                hours_above_min_alt=float(hours_arr[k]),
                session_count=int(cap["session_count"]) if cap else 0,
                last_session_at=cap["last_session_at"] if cap else None,
                alt_curve_deg=curve,
            )
        )

    out.sort(key=lambda e: e.alt_now_deg, reverse=True)
    return TonightResponse(
        at_utc=at_utc.isoformat(),
        site_latitude=float(site_lat),
        site_longitude=float(site_lon),
        site_elevation_m=float(site_elev),
        min_alt_deg=float(min_alt),
        max_magnitude=float(max_mag),
        dusk_utc=dusk_utc.isoformat() if dusk_utc else None,
        dawn_utc=dawn_utc.isoformat() if dawn_utc else None,
        alt_curve_step_min=sky.SPARKLINE_STEP_MIN,
        entries=out,
    )


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
                # Declared output ports + their types, so the UI can ask
                # for a preview at the right port (eg narrowband_extract has
                # `ha` / `oiii`, not `image`) without a parallel guess table.
                "outputs": {p: str(t) for p, t in node_cls.outputs.items()},
                "ui_depends_on": spec.ui_depends_on,
                "preview_display_ready": node_cls.preview_display_ready,
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
def get_preview(node_hash: str, port: str, neutral: int = 1) -> FileResponse:
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
    manifest = job_manager.cache.load_outputs(node_hash)
    display_ready = False
    if manifest is not None and port in manifest:
        display_ready = manifest[port].display_ready
    try:
        path = render_preview(
            job_manager.cache, node_hash, port, neutral=bool(neutral), display_ready=display_ready
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
