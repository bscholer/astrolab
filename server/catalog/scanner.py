"""Catalog scanner.

Walks a capture root via a registered ingest adapter, reads each FITS
header, and upserts frames / sessions / targets in SQLite. Incremental:
files whose (path, size, mtime) match a prior scan are skipped without
opening the FITS.

Hashing strategy: xxhash64 of file contents, computed only on first ingest
(or when size/mtime changed). xxhash is fast enough that 16MB FITS frames
hash in tens of ms each; the practical bottleneck is the FITS header parse
itself, not the hash.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import xxhash

from .adapter import DiscoveredFrame, DiscoveredMaster, IngestAdapter
from .adapter import lookup as adapter_lookup
from .db import open_db
from .fits_reader import normalize_target, read_primary_header
from .matching import match_all_sessions
from .openngc import enrich as openngc_enrich
from .sky_match import (
    FrameSky,
    auto_tolerance_deg,
    frame_fov_diagonal_deg,
    nearest_match,
    target_centroid,
    target_envelope,
)

log = logging.getLogger("astrolab.catalog.scanner")

ProgressFn = Callable[[int, int, str], None]
"""Called as progress(seen, total_estimate, current_path). total_estimate may
be 0 if the count is unknown (we're streaming)."""

XXHASH_BUF: int = 1 << 20  # 1 MiB


class ScanStats:
    def __init__(self) -> None:
        self.discovered: int = 0
        self.skipped_unchanged: int = 0
        self.inserted: int = 0
        self.updated: int = 0
        self.failed: int = 0
        self.removed: int = 0
        self.masters_inserted: int = 0
        self.masters_updated: int = 0
        self.masters_skipped: int = 0
        self.masters_removed: int = 0

    def __repr__(self) -> str:
        return (
            f"ScanStats(discovered={self.discovered}, "
            f"skipped={self.skipped_unchanged}, inserted={self.inserted}, "
            f"updated={self.updated}, removed={self.removed}, "
            f"failed={self.failed}, "
            f"masters[ins={self.masters_inserted} upd={self.masters_updated} "
            f"skip={self.masters_skipped} rm={self.masters_removed}])"
        )


def _hash_file(path: Path) -> str:
    h = xxhash.xxh64()
    with path.open("rb") as f:
        while True:
            chunk = f.read(XXHASH_BUF)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _existing_row(conn: sqlite3.Connection, path_str: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, size, mtime, file_hash FROM frames WHERE path = ?", (path_str,)
    ).fetchone()


def _ingest_frame(
    conn: sqlite3.Connection,
    discovered: DiscoveredFrame,
    scope_id: str,
    scan_started_at: float,
    stats: ScanStats,
) -> None:
    path = discovered.path
    path_str = str(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        log.warning("vanished mid-scan: %s", path)
        return

    existing = _existing_row(conn, path_str)
    if (
        existing is not None
        and existing["size"] == st.st_size
        and existing["mtime"] == st.st_mtime
        and existing["file_hash"] is not None
    ):
        # Touch scanned_at so the orphan-removal pass at the end of the scan
        # knows we observed this row this run.
        conn.execute(
            "UPDATE frames SET scanned_at = ? WHERE path = ?",
            (scan_started_at, path_str),
        )
        stats.skipped_unchanged += 1
        return

    try:
        header = read_primary_header(path)
    except Exception as exc:
        log.warning("failed to read FITS header: %s (%s)", path, exc)
        stats.failed += 1
        return

    file_hash = _hash_file(path)

    hints = discovered.session_hints or {}
    object_name_raw = header.get("OBJECT") or hints.get("target_from_path")
    object_name = normalize_target(object_name_raw) if object_name_raw else None

    row: dict[str, Any] = {
        "file_hash": file_hash,
        "path": path_str,
        "inode": st.st_ino,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "image_type": discovered.image_type,
        "quality": discovered.quality,
        "object": object_name,
        "instrument": header.get("INSTRUME"),
        "camera": header.get("CAMERA"),
        "filter": header.get("FILTER"),
        "exptime": _coerce_float(header.get("EXPTIME") or hints.get("exptime_from_path")),
        "gain": _coerce_int(header.get("GAIN") or hints.get("gain_from_path")),
        "binning": _coerce_int(header.get("XBINNING") or hints.get("binning_from_path")),
        "ccd_temp": _coerce_float(header.get("DET-TEMP") or header.get("CCD-TEMP")),
        "date_obs": header.get("DATE-OBS") or hints.get("started_at_from_path"),
        "ra": _coerce_float(header.get("RA")),
        "dec": _coerce_float(header.get("DEC")),
        "scope_id": scope_id,
        "session_key": discovered.session_key,
        "fits_headers": json.dumps(header).encode("utf-8"),
        "scanned_at": scan_started_at,
    }

    columns = list(row.keys())
    placeholders = ",".join("?" for _ in columns)
    sql = (
        f"INSERT INTO frames ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in columns)
    )
    conn.execute(sql, [row[c] for c in columns])
    if existing is None:
        stats.inserted += 1
    else:
        stats.updated += 1


def _upsert_target(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM targets WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute("INSERT INTO targets (name) VALUES (?)", (name,))
    return cur.lastrowid or -1


def _ingest_master(
    conn: sqlite3.Connection,
    discovered: DiscoveredMaster,
    scope_id: str,
    scan_started_at: float,
    stats: ScanStats,
) -> None:
    path = discovered.path
    path_str = str(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        log.warning("master vanished mid-scan: %s", path)
        return

    existing = conn.execute(
        "SELECT id, size, mtime, file_hash FROM masters WHERE path = ?",
        (path_str,),
    ).fetchone()
    if (
        existing is not None
        and existing["size"] == st.st_size
        and existing["mtime"] == st.st_mtime
        and existing["file_hash"] is not None
    ):
        conn.execute(
            "UPDATE masters SET scanned_at = ? WHERE path = ?",
            (scan_started_at, path_str),
        )
        stats.masters_skipped += 1
        return

    file_hash = _hash_file(path)
    row: dict[str, Any] = {
        "kind": discovered.kind,
        "scope_id": scope_id,
        "source": discovered.source,
        "instrument": discovered.instrument,
        "camera": discovered.camera,
        "filter": discovered.filter,
        "exptime": discovered.exptime,
        "gain": discovered.gain,
        "binning": discovered.binning,
        "ccd_temp": discovered.ccd_temp,
        "stack_count": discovered.stack_count,
        "file_hash": file_hash,
        "path": path_str,
        "inode": st.st_ino,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "date_built": None,
        "cache_ref": path_str,
        "source_frame_ids": None,
        "scanned_at": scan_started_at,
    }
    columns = list(row.keys())
    placeholders = ",".join("?" for _ in columns)
    sql = (
        f"INSERT INTO masters ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in columns)
    )
    conn.execute(sql, [row[c] for c in columns])
    if existing is None:
        stats.masters_inserted += 1
    else:
        stats.masters_updated += 1


def _remove_orphans(
    conn: sqlite3.Connection,
    scope_id: str,
    root: Path,
    scan_started_at: float,
    stats: ScanStats,
) -> None:
    """Delete frame and master rows for files that the adapter no longer surfaces.

    Scoped to the prefix we just scanned, so unrelated paths from other roots
    are not affected. Sessions and targets that lose all their members get
    cleaned up at the end of _refresh_sessions.
    """
    root_prefix = str(root.resolve()) + "/"
    cur = conn.execute(
        "DELETE FROM frames "
        "WHERE scope_id = ? AND substr(path, 1, ?) = ? "
        "AND (scanned_at IS NULL OR scanned_at < ?)",
        (scope_id, len(root_prefix), root_prefix, scan_started_at),
    )
    stats.removed = cur.rowcount or 0
    cur = conn.execute(
        "DELETE FROM masters "
        "WHERE scope_id = ? AND substr(path, 1, ?) = ? "
        "AND (scanned_at IS NULL OR scanned_at < ?)",
        (scope_id, len(root_prefix), root_prefix, scan_started_at),
    )
    stats.masters_removed = cur.rowcount or 0


def _refresh_sessions(conn: sqlite3.Connection, scope_id: str) -> None:
    """Recompute session rows from the frames table.

    We rebuild from scratch each scan rather than try to maintain incremental
    state, because frames can be removed or reclassified between scans and
    keeping per-session counters consistent is fiddly. The cost is one
    GROUP BY over frames per scan; cheap at the scales we care about.
    """
    keys = conn.execute(
        "SELECT DISTINCT session_key FROM frames "
        "WHERE session_key IS NOT NULL AND scope_id = ?",
        (scope_id,),
    ).fetchall()
    for r in keys:
        key = r["session_key"]
        agg = conn.execute(
            """
            SELECT
                MIN(date_obs) AS started_at,
                MAX(date_obs) AS ended_at,
                COUNT(*)      AS total,
                SUM(CASE WHEN quality = 'failed' THEN 1 ELSE 0 END) AS failed,
                MAX(object)   AS object,
                MAX(instrument) AS instrument,
                MAX(camera)   AS camera,
                MAX(filter)   AS filter,
                MAX(exptime)  AS exptime,
                MAX(gain)     AS gain,
                MAX(binning)  AS binning
            FROM frames
            WHERE session_key = ?
            """,
            (key,),
        ).fetchone()

        target_id: int | None = None
        if agg["object"]:
            target_id = _upsert_target(conn, agg["object"])

        conn.execute(
            """
            INSERT INTO sessions (
                scope_id, session_key, target_id, instrument, camera, filter,
                exptime, gain, binning, started_at, ended_at, frame_count, failed_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_key) DO UPDATE SET
                target_id    = excluded.target_id,
                instrument   = excluded.instrument,
                camera       = excluded.camera,
                filter       = excluded.filter,
                exptime      = excluded.exptime,
                gain         = excluded.gain,
                binning      = excluded.binning,
                started_at   = excluded.started_at,
                ended_at     = excluded.ended_at,
                frame_count  = excluded.frame_count,
                failed_count = excluded.failed_count
            """,
            (
                scope_id,
                key,
                target_id,
                agg["instrument"],
                agg["camera"],
                agg["filter"],
                agg["exptime"],
                agg["gain"],
                agg["binning"],
                agg["started_at"],
                agg["ended_at"],
                agg["total"],
                agg["failed"] or 0,
            ),
        )

        session_row = conn.execute(
            "SELECT id FROM sessions WHERE session_key = ?", (key,)
        ).fetchone()
        session_id = session_row["id"]

        # Re-link session_frames in one pass.
        conn.execute("DELETE FROM session_frames WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO session_frames (session_id, frame_id) "
            "SELECT ?, id FROM frames WHERE session_key = ?",
            (session_id, key),
        )

    # Drop sessions whose frames all vanished, then orphan targets.
    conn.execute(
        "DELETE FROM sessions WHERE session_key NOT IN "
        "(SELECT DISTINCT session_key FROM frames WHERE session_key IS NOT NULL)"
    )
    conn.execute(
        "DELETE FROM targets WHERE id NOT IN "
        "(SELECT DISTINCT target_id FROM sessions WHERE target_id IS NOT NULL)"
    )


def frames_for_target(
    conn: sqlite3.Connection, target_id: int
) -> list[FrameSky]:
    """Lift every frame belonging to a target into a FrameSky record.

    Reads RA/Dec straight from the frames row; reaches into the
    fits_headers JSON blob for FOCALLEN / XPIXSZ / YPIXSZ / NAXIS1 /
    NAXIS2 since those columns aren't promoted out of the blob yet
    (the scanner only promotes the headers exposed in the sessions
    UI). Frames whose blob fails to parse are dropped silently so a
    single corrupt row doesn't break the entire target's resolve.
    """
    rows = conn.execute(
        """
        SELECT f.ra AS ra, f.dec AS dec, f.fits_headers AS hdr
        FROM frames f
        JOIN sessions s ON s.session_key = f.session_key
        WHERE s.target_id = ?
        """,
        (target_id,),
    ).fetchall()
    out: list[FrameSky] = []
    for r in rows:
        ra, dec = r["ra"], r["dec"]
        focallen = xpix = ypix = n1 = n2 = None
        blob = r["hdr"]
        if blob is not None:
            try:
                hdr = json.loads(bytes(blob).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                hdr = {}
            focallen = hdr.get("FOCALLEN")
            xpix = hdr.get("XPIXSZ")
            ypix = hdr.get("YPIXSZ")
            n1 = hdr.get("NAXIS1")
            n2 = hdr.get("NAXIS2")
        out.append(
            FrameSky(
                ra_deg=ra,
                dec_deg=dec,
                fov_diagonal_deg=frame_fov_diagonal_deg(focallen, xpix, ypix, n1, n2),
            )
        )
    return out


def _clear_auto_resolve(conn: sqlite3.Connection, target_id: int) -> None:
    """Reset the auto-resolve columns for a target."""
    conn.execute(
        "UPDATE targets SET "
        "  resolved_canonical = NULL, "
        "  resolved_separation_arcmin = NULL, "
        "  resolved_source = NULL, "
        "  resolved_at = NULL "
        "WHERE id = ?",
        (target_id,),
    )


def resolve_target(conn: sqlite3.Connection, target_id: int, name: str) -> None:
    """Auto-resolve a single target's catalog mapping.

    Same name-first / position-fallback rule as `_resolve_targets`,
    extracted so the per-session reassign endpoint can re-run resolution
    against a freshly-created or freshly-rebound target without walking
    every row in the table.
    """
    hit = openngc_enrich(name) if name else None
    if hit is not None:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE targets SET "
            "  resolved_canonical = ?, "
            "  resolved_separation_arcmin = ?, "
            "  resolved_source = 'name', "
            "  resolved_at = ? "
            "WHERE id = ?",
            (hit.canonical, 0.0, now, target_id),
        )
        return

    frames = frames_for_target(conn, target_id)
    envelope = target_envelope(frames)
    if envelope is not None:
        ra_centroid, dec_centroid, env_deg = envelope
        tol = auto_tolerance_deg(env_deg)
    else:
        # No FOV headers anywhere on this target, but if the centroid is
        # still computable (frames have RA/Dec, just no focallen/pixsz),
        # match against the 1.0 deg fallback rather than giving up.
        centroid = target_centroid(frames)
        if centroid is None:
            _clear_auto_resolve(conn, target_id)
            return
        ra_centroid, dec_centroid = centroid
        tol = auto_tolerance_deg(None)

    match = nearest_match(ra_centroid, dec_centroid, tol)
    if match is None:
        _clear_auto_resolve(conn, target_id)
        return
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE targets SET "
        "  resolved_canonical = ?, "
        "  resolved_separation_arcmin = ?, "
        "  resolved_source = 'position', "
        "  resolved_at = ? "
        "WHERE id = ?",
        (match.canonical, match.separation_deg * 60.0, now, target_id),
    )


def _resolve_targets(conn: sqlite3.Connection) -> None:
    """Auto-resolve every target's catalog mapping.

    Name-first: if openngc.enrich(target.name) hits, we record that as
    `resolved_source='name'` with separation 0. Position-fallback: when
    name doesn't resolve, we compute the centroid + envelope from the
    target's frames and run a great-circle nearest-match within a
    scope-aware tolerance. On a miss, every resolved_* column goes back
    to null so a target that lost its frames doesn't keep claiming an
    old answer.
    """
    rows = conn.execute("SELECT id, name FROM targets").fetchall()
    for trow in rows:
        resolve_target(conn, trow["id"], trow["name"])


def scan(
    root: Path,
    *,
    scope_id: str,
    db_path: Path | None = None,
    progress: ProgressFn | None = None,
    adapter: IngestAdapter | None = None,
) -> ScanStats:
    """Scan a capture root and update the catalog.

    Returns ScanStats with counters. A fresh DB is created if needed.
    """
    on_progress = progress or (lambda _seen, _total, _path: None)
    chosen: IngestAdapter = adapter if adapter is not None else adapter_lookup(scope_id)

    scan_started_at = time.time()
    stats = ScanStats()
    with open_db(db_path) as conn:
        for discovered in chosen.discover(root):
            stats.discovered += 1
            on_progress(stats.discovered, 0, str(discovered.path))
            with conn:
                if isinstance(discovered, DiscoveredFrame):
                    _ingest_frame(conn, discovered, scope_id, scan_started_at, stats)
                elif isinstance(discovered, DiscoveredMaster):
                    _ingest_master(conn, discovered, scope_id, scan_started_at, stats)
        with conn:
            _remove_orphans(conn, scope_id, root, scan_started_at, stats)
            _refresh_sessions(conn, scope_id)
        with conn:
            match_all_sessions(conn)
        with conn:
            _resolve_targets(conn)
    log.info("scan complete: %r", stats)
    return stats
