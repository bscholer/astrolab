"""FastAPI application for the astrolab control plane.

Phase 1 only ships read-only catalog endpoints plus a POST /api/scan to
trigger a rescan. Job submission, WebSocket progress, and render endpoints
land in Phase 2+.

Endpoints:
- GET  /api/health                       liveness ping
- GET  /api/targets                      target list with frame counts
- GET  /api/targets/{id}                 target detail: sessions + calibration
- GET  /api/sessions/{id}                session detail: frames summary + cal
- POST /api/scan                         trigger a rescan (synchronous in 1.c)
- GET  /api/masters                      indexed calibration masters
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

import server.catalog.adapters  # noqa: F401  registers ingest adapters
from server.catalog.common_names import lookup as lookup_common_name
from server.catalog.db import open_db
from server.catalog.scanner import scan as run_scan

log = logging.getLogger("astrolab.api")

app = FastAPI(title="astrolab", version="0.1.0")

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


# Make `python -m server.api` start a dev server.
def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("server.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
