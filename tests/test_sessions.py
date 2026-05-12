"""Tests for the time-gap session clusterer.

These exercise the clusterer directly against a fresh in-memory catalog
rather than going through the scanner — keeps the failure modes localized
to grouping logic, separate from FITS-reading or hint-merging.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from server.catalog.db import connect
from server.catalog.sessions import cluster_sessions


def _insert_light(
    conn,
    *,
    target: str = "M 33",
    date_obs: str,
    filter_: str = "None",
    exptime: float = 30.0,
    gain: int = 60,
    instrument: str = "DWARFIII",
    camera: str = "TELE",
    binning: int = 1,
    scope_id: str = "dwarf3",
    quality: str = "ok",
    path: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO frames (
            path, image_type, quality, object, instrument, camera, filter,
            exptime, gain, binning, date_obs, scope_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            path or f"/x/{date_obs}.fits",
            "LIGHT",
            quality,
            target,
            instrument,
            camera,
            filter_,
            exptime,
            gain,
            binning,
            date_obs,
            scope_id,
        ),
    )
    return int(cur.lastrowid or -1)


def _iso_minutes(base: datetime, minutes: int) -> str:
    return (base + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def conn(tmp_path: Path):
    """Fresh on-disk catalog for one test."""
    c = connect(tmp_path / "cat.sqlite")
    yield c
    c.close()


# ---------- happy path ----------------------------------------------------


def test_contiguous_capture_is_one_session(conn) -> None:
    """A clean 30-frame capture with no gaps clusters as one session."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    for i in range(30):
        _insert_light(conn, date_obs=_iso_minutes(base, i), path=f"/x/{i}.fits")

    cluster_sessions(conn)

    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    assert len(sessions) == 1
    assert sessions[0]["frame_count"] == 30
    assert sessions[0]["failed_count"] == 0


def test_short_restart_gap_stays_one_session(conn) -> None:
    """A 5-minute pause mid-capture (restart, refocus) stays one session."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    for i in range(10):
        _insert_light(conn, date_obs=_iso_minutes(base, i), path=f"/a/{i}.fits")
    # 5-minute gap then another run.
    for i in range(10):
        _insert_light(
            conn, date_obs=_iso_minutes(base, 15 + i), path=f"/b/{i}.fits"
        )

    cluster_sessions(conn)

    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    assert len(sessions) == 1
    assert sessions[0]["frame_count"] == 20


def test_long_gap_splits_into_two_sessions(conn) -> None:
    """A 90-minute gap between frames opens a new session."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    for i in range(5):
        _insert_light(conn, date_obs=_iso_minutes(base, i), path=f"/a/{i}.fits")
    # 90-minute gap.
    for i in range(5):
        _insert_light(
            conn, date_obs=_iso_minutes(base, 95 + i), path=f"/b/{i}.fits"
        )

    cluster_sessions(conn)

    sessions = conn.execute(
        "SELECT frame_count FROM sessions ORDER BY started_at"
    ).fetchall()
    assert [s["frame_count"] for s in sessions] == [5, 5]


def test_multi_night_split(conn) -> None:
    """Same target, same filter, two consecutive nights -> two sessions."""
    night1 = datetime(2025, 10, 21, 22, 0, 0)
    night2 = datetime(2025, 10, 22, 22, 0, 0)
    for i in range(10):
        _insert_light(conn, date_obs=_iso_minutes(night1, i), path=f"/n1/{i}.fits")
        _insert_light(conn, date_obs=_iso_minutes(night2, i), path=f"/n2/{i}.fits")

    cluster_sessions(conn)

    sessions = conn.execute("SELECT frame_count FROM sessions").fetchall()
    assert [s["frame_count"] for s in sessions] == [10, 10]


def test_different_filters_split_into_separate_sessions(conn) -> None:
    """A simultaneous dual-filter capture (e.g. two cameras) still clusters
    into one session per filter."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    for i in range(5):
        _insert_light(
            conn, date_obs=_iso_minutes(base, i), filter_="None", path=f"/a/{i}.fits"
        )
        _insert_light(
            conn, date_obs=_iso_minutes(base, i), filter_="HaOIII", path=f"/b/{i}.fits"
        )

    cluster_sessions(conn)

    sessions = conn.execute(
        "SELECT filter, frame_count FROM sessions ORDER BY filter"
    ).fetchall()
    assert [(s["filter"], s["frame_count"]) for s in sessions] == [
        ("HaOIII", 5),
        ("None", 5),
    ]


def test_different_exposure_or_gain_splits_sessions(conn) -> None:
    """Two same-target captures back-to-back with different exposure end up
    as two sessions even with no time gap between them."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    for i in range(5):
        _insert_light(
            conn, date_obs=_iso_minutes(base, i), exptime=15.0, path=f"/a/{i}.fits"
        )
        _insert_light(
            conn, date_obs=_iso_minutes(base, i), exptime=60.0, path=f"/b/{i}.fits"
        )

    cluster_sessions(conn)

    sessions = conn.execute(
        "SELECT exptime, frame_count FROM sessions ORDER BY exptime"
    ).fetchall()
    assert [(s["exptime"], s["frame_count"]) for s in sessions] == [
        (15.0, 5),
        (60.0, 5),
    ]


# ---------- bookkeeping ---------------------------------------------------


def test_failed_frames_counted_separately(conn) -> None:
    """Failed subs belong to the session and contribute to failed_count."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    for i in range(8):
        _insert_light(conn, date_obs=_iso_minutes(base, i), path=f"/ok/{i}.fits")
    for i in range(2):
        _insert_light(
            conn,
            date_obs=_iso_minutes(base, 8 + i),
            quality="failed",
            path=f"/bad/{i}.fits",
        )

    cluster_sessions(conn)

    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    assert len(sessions) == 1
    assert sessions[0]["frame_count"] == 10
    assert sessions[0]["failed_count"] == 2


def test_links_session_frames(conn) -> None:
    """session_frames is the canonical many-to-many table; check linkage."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    frame_ids = [
        _insert_light(conn, date_obs=_iso_minutes(base, i), path=f"/x/{i}.fits")
        for i in range(3)
    ]

    cluster_sessions(conn)

    rows = conn.execute(
        "SELECT frame_id FROM session_frames ORDER BY frame_id"
    ).fetchall()
    assert [r["frame_id"] for r in rows] == sorted(frame_ids)


def test_creates_target_row(conn) -> None:
    base = datetime(2025, 10, 21, 22, 0, 0)
    _insert_light(conn, date_obs=_iso_minutes(base, 0), target="NGC 7000")

    cluster_sessions(conn)

    targets = conn.execute("SELECT name FROM targets").fetchall()
    assert {t["name"] for t in targets} == {"NGC 7000"}


def test_rerun_keeps_session_id_stable(conn) -> None:
    """Re-clustering the same data must preserve session.id so projects
    pinned to a session don't get orphaned by a routine rescan."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    for i in range(5):
        _insert_light(conn, date_obs=_iso_minutes(base, i), path=f"/x/{i}.fits")

    cluster_sessions(conn)
    first_id = conn.execute("SELECT id FROM sessions").fetchone()["id"]

    cluster_sessions(conn)
    second_id = conn.execute("SELECT id FROM sessions").fetchone()["id"]

    assert first_id == second_id


def test_orphaned_session_cleaned_up(conn) -> None:
    """A session whose frames all vanish (re-classified, deleted, etc.) is
    removed on the next cluster pass."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    fid = _insert_light(conn, date_obs=_iso_minutes(base, 0))
    cluster_sessions(conn)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1

    # Reclassify the only frame as a DARK; cluster again.
    conn.execute("UPDATE frames SET image_type = 'DARK' WHERE id = ?", (fid,))
    cluster_sessions(conn)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_frames_without_object_or_date_dropped(conn) -> None:
    """Lights missing target or DATE-OBS can't be clustered. They stay in
    the frames table but don't create a session row."""
    _insert_light(conn, date_obs="2025-10-21T22:00:00", target="M 33")
    conn.execute(
        """
        INSERT INTO frames (path, image_type, quality, object, date_obs, scope_id)
        VALUES ('/no_date.fits', 'LIGHT', 'ok', 'M 33', NULL, 'dwarf3')
        """
    )

    cluster_sessions(conn)

    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_gap_minutes_configurable(conn) -> None:
    """Caller can tighten or loosen the session boundary."""
    base = datetime(2025, 10, 21, 22, 0, 0)
    _insert_light(conn, date_obs=_iso_minutes(base, 0), path="/a.fits")
    _insert_light(conn, date_obs=_iso_minutes(base, 30), path="/b.fits")  # 30-min gap

    cluster_sessions(conn, gap_minutes=20)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2

    cluster_sessions(conn, gap_minutes=60)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
