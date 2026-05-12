"""Session derivation by time-gap clustering.

A "session" is a contiguous run of light frames captured under one
configuration: same target, same filter, same exposure / gain / instrument
/ camera / binning. Capture programs and folder layouts vary wildly across
scopes; rather than try to derive sessions from path conventions, we
re-derive them every scan from frame metadata directly.

Within one configuration we sort frames by ``DATE-OBS`` and split into
clusters wherever the gap between consecutive frames exceeds
``DEFAULT_GAP_MINUTES``. The default of 60 minutes captures two real
behaviors:

- **Mid-capture restart**: a short pause (a few minutes) to power-cycle
  or refocus stays one session, which is what the user wants — they're
  still imaging the same target on the same night.
- **Pack up and come back later**: a one-hour-plus gap reliably means
  the user moved scopes / took a break / re-set up, which is a new
  session even if the rest of the configuration is identical.

Idempotence
-----------

``cluster_sessions`` rebuilds the sessions table from scratch each call.
``session_key`` is the join column other tables use to refer to sessions,
so we compute it deterministically from the cluster's identifying tuple
(config + first frame's timestamp). As long as the cluster's earliest
frame doesn't shift on re-scan, the key — and therefore the session.id —
stays stable across scans, which preserves project references to
sessions.

A note on session-id stability: a cluster's earliest frame can shift if a
back-dated frame arrives in a later scan and joins the cluster. The
session_key shifts with it, the row gets replaced, and any project
pinned to the previous session.id is orphaned. We accept this for v1 in
exchange for simplicity; the proper fix (match new clusters to existing
sessions by frame-id overlap) lands when ``session_key`` is dropped
entirely.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import datetime

DEFAULT_GAP_MINUTES = 60
"""Time gap above which consecutive frames open a new session."""


# Identifying tuple for one capture configuration. Frames sharing all of
# these can theoretically belong to one session; the time-gap clustering
# step decides whether they actually do.
ConfigKey = tuple[
    str | None,   # scope_id
    str | None,   # object (target, normalized)
    str | None,   # instrument
    str | None,   # camera
    str | None,   # filter (canonical)
    float | None, # exptime
    int | None,   # gain
    int | None,   # binning
]


@dataclass
class _ClusterAgg:
    frame_ids: list[int]
    failed_count: int
    started_at: str
    ended_at: str


def _parse_iso(value: str) -> datetime | None:
    """Parse a DATE-OBS string. Returns None on anything we can't read.

    FITS DATE-OBS is "YYYY-MM-DDTHH:MM:SS[.fff][Z|+HH:MM]". Python's
    fromisoformat handles all those forms on 3.11+. Older / weirder
    formats fall through to None and that frame becomes its own cluster
    (one-frame session) which is the safest default behavior.
    """
    if not value:
        return None
    try:
        # fromisoformat doesn't accept the trailing "Z" pre-3.11 fully.
        # Strip it; UTC offset of 0 is the same as naive for ordering.
        cleaned = value.rstrip("Z")
        return datetime.fromisoformat(cleaned)
    except (TypeError, ValueError):
        return None


def cluster_sessions(
    conn: sqlite3.Connection, *, gap_minutes: int = DEFAULT_GAP_MINUTES
) -> None:
    """Rebuild sessions and session_frames from the current frames table.

    Lights only; darks/flats/bias have ``session_key`` left null in frames
    and don't appear here. Frames missing ``object`` or ``date_obs`` can't
    be placed and are silently dropped from session derivation (they
    remain in frames; they just don't get linked to a session row).
    """
    rows = conn.execute(
        """
        SELECT id, scope_id, object, instrument, camera, filter,
               exptime, gain, binning, date_obs, quality
        FROM frames
        WHERE image_type = 'LIGHT'
          AND object IS NOT NULL
          AND date_obs IS NOT NULL
        """
    ).fetchall()

    # Group by configuration.
    groups: dict[ConfigKey, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key: ConfigKey = (
            r["scope_id"],
            r["object"],
            r["instrument"],
            r["camera"],
            r["filter"],
            r["exptime"],
            r["gain"],
            r["binning"],
        )
        groups[key].append(r)

    # For each configuration group, sort by DATE-OBS and split on gaps.
    clusters: list[tuple[ConfigKey, _ClusterAgg]] = []
    threshold_seconds = gap_minutes * 60
    for config, frames in groups.items():
        sortable = sorted(
            frames,
            key=lambda row: (_parse_iso(row["date_obs"]) or datetime.min, row["id"]),
        )
        current: _ClusterAgg | None = None
        last_dt: datetime | None = None
        for f in sortable:
            dt = _parse_iso(f["date_obs"])
            if current is None or dt is None or last_dt is None:
                new_cluster = True
            else:
                new_cluster = (dt - last_dt).total_seconds() > threshold_seconds
            if new_cluster:
                if current is not None:
                    clusters.append((config, current))
                current = _ClusterAgg(
                    frame_ids=[f["id"]],
                    failed_count=1 if f["quality"] == "failed" else 0,
                    started_at=f["date_obs"],
                    ended_at=f["date_obs"],
                )
            else:
                assert current is not None
                current.frame_ids.append(f["id"])
                if f["quality"] == "failed":
                    current.failed_count += 1
                current.ended_at = f["date_obs"]
            last_dt = dt
        if current is not None:
            clusters.append((config, current))

    # Rebuild sessions table. We upsert by session_key so existing IDs
    # survive a rescan when the cluster identity is unchanged.
    rebuilt_keys: set[str] = set()
    for config, agg in clusters:
        scope_id, target, instrument, camera, filter_, exptime, gain, binning = config
        session_key = _make_session_key(config, agg.started_at)
        rebuilt_keys.add(session_key)
        target_id = _upsert_target(conn, target) if target else None
        conn.execute(
            """
            INSERT INTO sessions (
                scope_id, session_key, target_id, instrument, camera, filter,
                exptime, gain, binning, started_at, ended_at,
                frame_count, failed_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_key) DO UPDATE SET
                scope_id     = excluded.scope_id,
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
                session_key,
                target_id,
                instrument,
                camera,
                filter_,
                exptime,
                gain,
                binning,
                agg.started_at,
                agg.ended_at,
                len(agg.frame_ids),
                agg.failed_count,
            ),
        )
        session_id = conn.execute(
            "SELECT id FROM sessions WHERE session_key = ?", (session_key,)
        ).fetchone()["id"]

        # Re-link session_frames in one pass. We also write the
        # session_key back to the frames table so legacy joins through
        # frames.session_key (api.py, matching.py) keep working until
        # Phase 6 drops the column.
        conn.execute("DELETE FROM session_frames WHERE session_id = ?", (session_id,))
        placeholders = ",".join("?" for _ in agg.frame_ids)
        conn.execute(
            f"INSERT INTO session_frames (session_id, frame_id) "
            f"SELECT ?, id FROM frames WHERE id IN ({placeholders})",
            (session_id, *agg.frame_ids),
        )
        conn.execute(
            f"UPDATE frames SET session_key = ? WHERE id IN ({placeholders})",
            (session_key, *agg.frame_ids),
        )

    # Drop sessions whose cluster no longer exists, then clear session_key
    # on frames that didn't end up in any cluster (orphan lights — e.g. a
    # frame whose target was just cleared, or a dark mis-flagged as LIGHT
    # and now reclassified).
    if rebuilt_keys:
        placeholders = ",".join("?" for _ in rebuilt_keys)
        conn.execute(
            f"DELETE FROM sessions WHERE session_key NOT IN ({placeholders})",
            tuple(rebuilt_keys),
        )
        conn.execute(
            f"UPDATE frames SET session_key = NULL "
            f"WHERE image_type = 'LIGHT' AND session_key NOT IN ({placeholders})",
            tuple(rebuilt_keys),
        )
    else:
        conn.execute("DELETE FROM sessions")
        conn.execute(
            "UPDATE frames SET session_key = NULL WHERE image_type = 'LIGHT'"
        )

    # Targets whose sessions all vanished are orphans now.
    conn.execute(
        "DELETE FROM targets WHERE id NOT IN "
        "(SELECT DISTINCT target_id FROM sessions WHERE target_id IS NOT NULL)"
    )


def _make_session_key(config: ConfigKey, started_at: str) -> str:
    """Deterministic session identifier for cross-table joins.

    Includes the first frame's DATE-OBS so two captures of the same
    configuration on different nights produce different sessions.
    """
    scope_id, target, instrument, camera, filter_, exptime, gain, binning = config
    parts: tuple[Hashable, ...] = (
        scope_id or "",
        target or "",
        instrument or "",
        camera or "",
        filter_ or "",
        f"{exptime:g}" if exptime is not None else "",
        gain if gain is not None else "",
        binning if binning is not None else "",
        started_at,
    )
    return "|".join(str(p) for p in parts)


def _upsert_target(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM targets WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO targets (name) VALUES (?)", (name,))
    return int(cur.lastrowid or -1)


