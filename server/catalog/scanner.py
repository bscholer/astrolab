"""Catalog scanner.

Walks a capture root, reads each FITS primary header, runs it through the
universal classifier (see ``classify.py``) to decide scope and image type,
and upserts frames / sessions / targets in SQLite. Incremental: files
whose (path, size, mtime) match a prior scan are skipped without opening
the FITS.

The walker is layout-agnostic — it just recursively globs ``*.fits`` and
``*.fit`` under the root. Per-file scope detection means one root can mix
Dwarf 3, NINA, ASIAIR, and Seestar captures without any configuration.

Factory calibration masters (currently Dwarf 3's ``CALI_FRAME/`` tree) are
walked through a small scope-specific helper because their headers are
too sparse for the universal classifier to read; the rest of the masters
pipeline is shared with the frames path.

Sessions are derived from the frames table at the end of every scan via
``cluster_sessions`` (see ``sessions.py``) rather than being inferred at
ingest time from folder names. Time-gap clustering tolerates restart-
mid-capture pauses and folds them into one session.

Hashing strategy: xxhash64 of file contents, computed only on first
ingest (or when size/mtime changed). xxhash is fast enough that 16MB FITS
frames hash in tens of ms each; the practical bottleneck is the FITS
header parse itself, not the hash.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import xxhash

from .adapters import dwarf3 as dwarf3_adapter
from .classify import classify
from .db import open_db
from .filter_aliases import canonicalize as canonicalize_filter
from .fits_reader import normalize_target, read_primary_header
from .matching import match_all_sessions
from .models import DiscoveredMaster
from .openngc import enrich as openngc_enrich
from .sessions import cluster_sessions
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
        self.skipped_unknown: int = 0
        self.inserted: int = 0
        self.updated: int = 0
        self.failed: int = 0
        self.removed: int = 0
        self.masters_inserted: int = 0
        self.masters_updated: int = 0
        self.masters_skipped: int = 0
        self.masters_removed: int = 0
        self.scope_breakdown: dict[str, int] = defaultdict(int)
        """Frames ingested per detected scope_id. Surfaced by the API so the
        UI can warn when non-Dwarf-3 scopes are present (only Dwarf 3 is
        validated end-to-end through processing today)."""

    def __repr__(self) -> str:
        breakdown = (
            ",".join(f"{k}={v}" for k, v in sorted(self.scope_breakdown.items()))
            or "-"
        )
        return (
            f"ScanStats(discovered={self.discovered}, "
            f"skipped={self.skipped_unchanged}, unknown={self.skipped_unknown}, "
            f"inserted={self.inserted}, updated={self.updated}, "
            f"removed={self.removed}, failed={self.failed}, scopes=[{breakdown}], "
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
    path: Path,
    header: dict[str, Any],
    classified: Any,
    st: os.stat_result,
    scan_started_at: float,
    stats: ScanStats,
) -> None:
    """Upsert one frame given a stat and classifier decision.

    ``session_key`` is left null on insert; the cluster pass at the end of
    a scan writes it back as part of session derivation.
    """
    path_str = str(path)
    existing = _existing_row(conn, path_str)
    file_hash = _hash_file(path)

    object_name_raw = header.get("OBJECT")
    object_name = normalize_target(object_name_raw) if object_name_raw else None

    row: dict[str, Any] = {
        "file_hash": file_hash,
        "path": path_str,
        "inode": st.st_ino,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "image_type": classified.image_type,
        "quality": classified.quality,
        "object": object_name,
        "instrument": header.get("INSTRUME"),
        "camera": header.get("CAMERA"),
        "filter": canonicalize_filter(header.get("FILTER")),
        "exptime": _coerce_float(header.get("EXPTIME") or header.get("EXPOSURE")),
        "gain": _coerce_int(header.get("GAIN")),
        "binning": _coerce_int(header.get("XBINNING")),
        "ccd_temp": _coerce_float(
            header.get("DET-TEMP") or header.get("CCD-TEMP") or header.get("SET-TEMP")
        ),
        "date_obs": header.get("DATE-OBS"),
        "ra": _coerce_float(header.get("RA")),
        "dec": _coerce_float(header.get("DEC")),
        "scope_id": classified.scope_id,
        "session_key": None,
        "fits_headers": json.dumps(header).encode("utf-8"),
        "scanned_at": scan_started_at,
    }

    columns = list(row.keys())
    placeholders = ",".join("?" for _ in columns)
    sql = (
        f"INSERT INTO frames ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in columns if c != "session_key")
    )
    conn.execute(sql, [row[c] for c in columns])
    if existing is None:
        stats.inserted += 1
    else:
        stats.updated += 1
    stats.scope_breakdown[classified.scope_id] += 1


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
        "filter": canonicalize_filter(discovered.filter),
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
    root: Path,
    scan_started_at: float,
    stats: ScanStats,
) -> None:
    """Delete frame and master rows whose files no longer exist under ``root``.

    Scoped to the prefix we just scanned, so unrelated paths from other
    capture roots aren't affected. The scan timestamp filter is what
    distinguishes survivors from corpses: every file we observed this run
    has its ``scanned_at`` bumped, so anything still bearing an older
    timestamp under our prefix has vanished from disk.

    Multi-scope-aware: with per-file scope detection any subtree may carry
    frames from multiple scopes, so we don't filter by scope_id here.
    """
    root_prefix = str(root.resolve()) + "/"
    cur = conn.execute(
        "DELETE FROM frames "
        "WHERE substr(path, 1, ?) = ? "
        "AND (scanned_at IS NULL OR scanned_at < ?)",
        (len(root_prefix), root_prefix, scan_started_at),
    )
    stats.removed = cur.rowcount or 0
    cur = conn.execute(
        "DELETE FROM masters "
        "WHERE substr(path, 1, ?) = ? "
        "AND (scanned_at IS NULL OR scanned_at < ?)",
        (len(root_prefix), root_prefix, scan_started_at),
    )
    stats.masters_removed = cur.rowcount or 0


# Session derivation is now in server.catalog.sessions.cluster_sessions —
# time-gap clustering replaces folder-name-driven grouping. See
# sessions.py for the algorithm and trade-offs.


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


def _walk_fits(root: Path) -> Iterator[Path]:
    """Yield every ``*.fits`` / ``*.fit`` file under ``root`` (case-insensitive).

    Uses ``os.scandir`` for cheap recursion. We don't follow symlinks to
    avoid getting stuck in a cycle inside someone's home directory; users
    who need cross-disk captures can mount the volumes properly.
    """
    if not root.exists():
        return
    stack: list[Path] = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except (PermissionError, FileNotFoundError):
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    name = entry.name.lower()
                    if name.endswith(".fits") or name.endswith(".fit"):
                        yield Path(entry.path)
            except OSError:
                continue




_INCREMENTAL_CLUSTER_EVERY = 50
"""Re-cluster sessions every N freshly-ingested light frames so the library
populates progressively while a long scan runs. Cheap enough — one GROUP
BY over the frames table per N reads — to not slow down the FITS-reading
hot loop."""


def scan(
    root: Path,
    *,
    db_path: Path | None = None,
    progress: ProgressFn | None = None,
) -> ScanStats:
    """Scan a capture root and update the catalog.

    Per-file scope detection: there is no ``scope_id`` argument. Every
    FITS under ``root`` is classified individually, which lets one root
    mix multiple capture programs without surprise.

    Returns ScanStats including a per-scope frame count in
    ``scope_breakdown`` so the API can surface a warning about non-Dwarf-3
    scopes (only Dwarf 3 is end-to-end validated through processing).
    """
    on_progress = progress or (lambda _seen, _total, _path: None)
    scan_started_at = time.time()
    stats = ScanStats()
    frames_since_cluster = 0

    with open_db(db_path) as conn:
        for fits_path in _walk_fits(root):
            stats.discovered += 1
            on_progress(stats.discovered, 0, str(fits_path))

            try:
                st = fits_path.stat()
            except FileNotFoundError:
                log.warning("vanished mid-scan: %s", fits_path)
                continue

            existing = _existing_row(conn, str(fits_path))
            if (
                existing is not None
                and existing["size"] == st.st_size
                and existing["mtime"] == st.st_mtime
                and existing["file_hash"] is not None
            ):
                with conn:
                    conn.execute(
                        "UPDATE frames SET scanned_at = ? WHERE path = ?",
                        (scan_started_at, str(fits_path)),
                    )
                stats.skipped_unchanged += 1
                continue

            try:
                header = read_primary_header(fits_path)
            except Exception as exc:
                log.warning("failed to read FITS header: %s (%s)", fits_path, exc)
                stats.failed += 1
                continue

            classified = classify(header, fits_path)
            if classified is None:
                stats.skipped_unknown += 1
                continue

            # Dwarf 3 user darks carry stale OBJECT/RA/DEC from the
            # previous light capture in their headers and occasionally
            # miss EXPTIME/GAIN/DATE-OBS. The filename is authoritative
            # for those frames; enrich here before ingest.
            if classified.scope_id == "dwarf3" and classified.image_type == "DARK":
                header = dwarf3_adapter.enrich_dark_header(header, fits_path)

            with conn:
                _ingest_frame(
                    conn, fits_path, header, classified, st, scan_started_at, stats
                )
            frames_since_cluster += 1

            if frames_since_cluster >= _INCREMENTAL_CLUSTER_EVERY:
                with conn:
                    cluster_sessions(conn)
                    _resolve_targets(conn)
                frames_since_cluster = 0

        # Dwarf 3 factory masters live under CALI_FRAME/ with sparse
        # headers; the universal walker passes over them (classify()
        # returns None). Walk them through the scope-specific helper.
        for master in dwarf3_adapter.walk_factory_masters(root):
            with conn:
                _ingest_master(conn, master, "dwarf3", scan_started_at, stats)

        with conn:
            _remove_orphans(conn, root, scan_started_at, stats)
            cluster_sessions(conn)
        with conn:
            match_all_sessions(conn)
        with conn:
            _resolve_targets(conn)

    log.info("scan complete: %r", stats)
    return stats
