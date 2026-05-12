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
  or refocus stays one session, which is what the user wants - they're
  still imaging the same target on the same night.
- **Pack up and come back later**: a one-hour-plus gap reliably means
  the user moved scopes / took a break / re-set up, which is a new
  session even if the rest of the configuration is identical.

Session ID stability across rescans
-----------------------------------

Re-running ``cluster_sessions`` against a frames table whose contents
have grown (the common case - incremental scans add new frames) must
preserve ``sessions.id`` for clusters whose identity is unchanged.
Projects pin to ``sessions.id``, and reissuing IDs every scan would
orphan them.

We match new clusters to existing sessions by **frame-id overlap**: for
each new cluster, the existing session sharing the most frame IDs with
it wins and keeps its row. Ties are broken arbitrarily. New clusters
with no overlap to any existing session get a fresh row. Existing
sessions matched by no cluster are deleted (their frames were
reclassified, removed, or moved to a different cluster).

The overlap match handles back-dated frames, mid-cluster merges, and
small splits correctly without depending on any frame's identity being
stable. The cost is one ``GROUP BY`` over ``session_frames`` per cluster,
which is negligible at any realistic scale.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
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
class _Cluster:
    config: ConfigKey
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


def _compute_clusters(
    conn: sqlite3.Connection, gap_minutes: int
) -> list[_Cluster]:
    """Read the frames table and return one ``_Cluster`` per derived session."""
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

    threshold_seconds = gap_minutes * 60
    clusters: list[_Cluster] = []
    for config, frames in groups.items():
        sortable = sorted(
            frames,
            key=lambda row: (_parse_iso(row["date_obs"]) or datetime.min, row["id"]),
        )
        current: _Cluster | None = None
        last_dt: datetime | None = None
        for f in sortable:
            dt = _parse_iso(f["date_obs"])
            if current is None or dt is None or last_dt is None:
                new_cluster = True
            else:
                new_cluster = (dt - last_dt).total_seconds() > threshold_seconds
            if new_cluster:
                if current is not None:
                    clusters.append(current)
                current = _Cluster(
                    config=config,
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
            clusters.append(current)
    return clusters


def _existing_session_frames(conn: sqlite3.Connection) -> dict[int, set[int]]:
    """``{session_id: set(frame_id)}`` for every session.

    Sessions whose frames all vanished (CASCADE-deleted from
    ``session_frames``) still appear here as ``{sid: set()}`` so the
    orphan-cleanup pass at the end of ``cluster_sessions`` notices and
    drops them.
    """
    out: dict[int, set[int]] = defaultdict(set)
    for r in conn.execute("SELECT id FROM sessions"):
        out[r["id"]] = set()
    for r in conn.execute("SELECT session_id, frame_id FROM session_frames"):
        out[r["session_id"]].add(r["frame_id"])
    return out


def _match_cluster_to_session(
    cluster_frames: Iterable[int],
    existing: dict[int, set[int]],
    consumed: set[int],
) -> int | None:
    """Pick the existing session with maximum frame overlap to ``cluster_frames``.

    ``consumed`` tracks session IDs already claimed by an earlier cluster
    in this rebuild - one session can't be reused by two clusters.
    Returns the chosen session_id, or ``None`` if no existing session
    shares any frames with this cluster (a brand-new session).
    """
    cluster_set = set(cluster_frames)
    best_id: int | None = None
    best_overlap = 0
    for sid, frame_ids in existing.items():
        if sid in consumed:
            continue
        overlap = len(cluster_set & frame_ids)
        if overlap > best_overlap:
            best_overlap = overlap
            best_id = sid
    return best_id


def cluster_sessions(
    conn: sqlite3.Connection, *, gap_minutes: int = DEFAULT_GAP_MINUTES
) -> None:
    """Rebuild sessions and session_frames from the current frames table.

    Lights only; darks/flats/bias do not appear as sessions. Frames
    missing ``object`` or ``date_obs`` can't be placed and are silently
    dropped from session derivation.
    """
    clusters = _compute_clusters(conn, gap_minutes)
    existing = _existing_session_frames(conn)
    consumed: set[int] = set()

    for cluster in clusters:
        scope_id, target, instrument, camera, filter_, exptime, gain, binning = (
            cluster.config
        )
        target_id = _upsert_target(conn, target) if target else None
        matched_id = _match_cluster_to_session(cluster.frame_ids, existing, consumed)
        if matched_id is not None:
            consumed.add(matched_id)
            conn.execute(
                """
                UPDATE sessions SET
                    scope_id = ?, target_id = ?, instrument = ?, camera = ?,
                    filter = ?, exptime = ?, gain = ?, binning = ?,
                    started_at = ?, ended_at = ?,
                    frame_count = ?, failed_count = ?
                WHERE id = ?
                """,
                (
                    scope_id, target_id, instrument, camera,
                    filter_, exptime, gain, binning,
                    cluster.started_at, cluster.ended_at,
                    len(cluster.frame_ids), cluster.failed_count,
                    matched_id,
                ),
            )
            session_id = matched_id
        else:
            cur = conn.execute(
                """
                INSERT INTO sessions (
                    scope_id, target_id, instrument, camera, filter,
                    exptime, gain, binning, started_at, ended_at,
                    frame_count, failed_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    scope_id, target_id, instrument, camera, filter_,
                    exptime, gain, binning,
                    cluster.started_at, cluster.ended_at,
                    len(cluster.frame_ids), cluster.failed_count,
                ),
            )
            session_id = int(cur.lastrowid or -1)

        # Re-link session_frames for this session in one pass.
        conn.execute("DELETE FROM session_frames WHERE session_id = ?", (session_id,))
        placeholders = ",".join("?" for _ in cluster.frame_ids)
        conn.execute(
            f"INSERT INTO session_frames (session_id, frame_id) "
            f"SELECT ?, id FROM frames WHERE id IN ({placeholders})",
            (session_id, *cluster.frame_ids),
        )

    # Existing sessions not matched to any cluster have lost all their
    # frames - drop them. CASCADE on session_frames does the link cleanup.
    orphans = [sid for sid in existing if sid not in consumed]
    if orphans:
        placeholders = ",".join("?" for _ in orphans)
        conn.execute(
            f"DELETE FROM sessions WHERE id IN ({placeholders})", tuple(orphans)
        )

    # Targets whose sessions all vanished are orphans now.
    conn.execute(
        "DELETE FROM targets WHERE id NOT IN "
        "(SELECT DISTINCT target_id FROM sessions WHERE target_id IS NOT NULL)"
    )


def _upsert_target(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM targets WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO targets (name) VALUES (?)", (name,))
    return int(cur.lastrowid or -1)
