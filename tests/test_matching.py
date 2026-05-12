"""Calibration matching tests.

We seed the DB with hand-crafted masters and a session row, then run
match_session and assert the chosen master + quality.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from server.catalog.db import connect
from server.catalog.matching import match_session


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "cat.sqlite")
    try:
        yield conn
    finally:
        conn.close()


def _insert_session(
    conn: sqlite3.Connection,
    *,
    instrument: str = "DWARFIII",
    camera: str = "TELE",
    filter_: str = "Astro",
    exptime: float = 30.0,
    gain: int = 60,
    binning: int = 1,
    target_name: str = "M 33",
    avg_temp: float | None = 24.0,
) -> int:
    target_cur = conn.execute("INSERT INTO targets (name) VALUES (?)", (target_name,))
    target_id = target_cur.lastrowid
    cur = conn.execute(
        """INSERT INTO sessions (
            scope_id, target_id, instrument, camera, filter,
            exptime, gain, binning, frame_count, failed_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("dwarf3", target_id, instrument, camera, filter_,
         exptime, gain, binning, 10, 0),
    )
    sid = int(cur.lastrowid or -1)
    if avg_temp is not None:
        # Seed at least one frame so the matcher's avg_ccd_temp subquery resolves.
        # Link it through session_frames so the matcher's join finds it.
        frame_cur = conn.execute(
            """INSERT INTO frames (path, image_type, scope_id, instrument,
                                   camera, filter, exptime, gain, binning, ccd_temp)
               VALUES (?, 'LIGHT', 'dwarf3', ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"/tmp/seed-{sid}.fits",
                instrument, camera, filter_, exptime, gain, binning, avg_temp,
            ),
        )
        conn.execute(
            "INSERT INTO session_frames (session_id, frame_id) VALUES (?, ?)",
            (sid, frame_cur.lastrowid),
        )
    return sid


def _insert_master(
    conn: sqlite3.Connection,
    *,
    kind: str,
    exptime: float | None = None,
    gain: int | None = 60,
    binning: int | None = 1,
    ccd_temp: float | None = None,
    instrument: str = "DWARFIII",
    camera: str = "TELE",
    filter_: str | None = None,
    stack_count: int = 10,
    path: str | None = None,
    date_built: str | None = None,
) -> int:
    p = path or f"/tmp/master-{kind}-{exptime}-{ccd_temp}.fits"
    conn.execute(
        """INSERT INTO masters
            (kind, scope_id, source, instrument, camera, filter, exptime, gain,
             binning, ccd_temp, stack_count, path, date_built)
           VALUES (?, 'dwarf3', 'factory', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, instrument, camera, filter_, exptime, gain, binning, ccd_temp,
         stack_count, p, date_built),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def test_dark_exact_match(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, exptime=30.0, avg_temp=22.0)
    spot_on = _insert_master(db, kind="dark", exptime=30.0, ccd_temp=22.0, stack_count=10)
    _insert_master(db, kind="dark", exptime=30.0, ccd_temp=20.0, stack_count=20)

    out = match_session(db, sid)
    assert out["dark"]["master_id"] == spot_on
    assert out["dark"]["match_quality"] == "exact"


def test_dark_approx_match_within_tolerance(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, exptime=30.0, avg_temp=22.0)
    near = _insert_master(db, kind="dark", exptime=30.0, ccd_temp=23.0, stack_count=8)
    far = _insert_master(db, kind="dark", exptime=30.0, ccd_temp=19.0, stack_count=20)

    out = match_session(db, sid)
    assert out["dark"]["master_id"] == near, "closer temp bin wins even with smaller stack"
    assert out["dark"]["match_quality"] == "approx"
    assert abs(out["dark"]["details"]["delta_C"] - 1.0) < 1e-9
    assert far is not None  # exists but rejected


def test_dark_approx_tie_breaks_on_stack_depth(db: sqlite3.Connection) -> None:
    """When two darks are equidistant, prefer the deeper stack."""
    sid = _insert_session(db, exptime=30.0, avg_temp=22.0)
    shallow = _insert_master(db, kind="dark", exptime=30.0, ccd_temp=24.0, stack_count=3)
    deep = _insert_master(db, kind="dark", exptime=30.0, ccd_temp=20.0, stack_count=20)
    out = match_session(db, sid)
    assert out["dark"]["master_id"] == deep
    assert shallow is not None


def test_dark_exact_temp_shallow_loses_to_nearby_deep_stack(db: sqlite3.Connection) -> None:
    """A deeper stack 1 C off beats a shallower stack at the exact session temp.

    Regression for the Dwarf 3 factory dark problem: the on-device stacker
    produced a 3-frame master at the exact session temperature, but a 10-frame
    master existed 1 C away.  The 1-C delta falls in the same 1-C bin as a
    0-C delta, so stack_count is the deciding factor and the deeper master must
    win.
    """
    sid = _insert_session(db, exptime=15.0, avg_temp=23.0)
    shallow_exact = _insert_master(db, kind="dark", exptime=15.0, ccd_temp=23.0, stack_count=3,
                                   path="/tmp/dark-exact-shallow.fits")
    deep_nearby = _insert_master(db, kind="dark", exptime=15.0, ccd_temp=24.0, stack_count=10,
                                 path="/tmp/dark-nearby-deep.fits")

    out = match_session(db, sid)
    assert out["dark"]["master_id"] == deep_nearby, (
        "stack_10 @ +1C should beat stack_3 @ exact temp"
    )
    assert out["dark"]["match_quality"] == "approx"
    assert shallow_exact is not None  # candidate existed but lost


def test_dark_no_match_when_temp_too_far(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, exptime=30.0, avg_temp=10.0)
    _insert_master(db, kind="dark", exptime=30.0, ccd_temp=24.0)
    _insert_master(db, kind="dark", exptime=30.0, ccd_temp=30.0)

    out = match_session(db, sid)
    assert out["dark"]["master_id"] is None
    assert out["dark"]["match_quality"] == "none"


def test_dark_skipped_when_exposure_does_not_match(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, exptime=30.0, avg_temp=22.0)
    _insert_master(db, kind="dark", exptime=15.0, ccd_temp=22.0)

    out = match_session(db, sid)
    assert out["dark"]["master_id"] is None
    assert out["dark"]["match_quality"] == "none"


def test_dark_skipped_when_gain_differs(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, exptime=30.0, gain=60, avg_temp=22.0)
    _insert_master(db, kind="dark", exptime=30.0, gain=80, ccd_temp=22.0)

    out = match_session(db, sid)
    assert out["dark"]["match_quality"] == "none"


def test_bias_match_when_present(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, gain=60, binning=1)
    bias = _insert_master(db, kind="bias", gain=60, binning=1, exptime=0.000125)
    out = match_session(db, sid)
    assert out["bias"]["master_id"] == bias
    assert out["bias"]["match_quality"] == "exact"


def test_bias_factory_matches_despite_photographic_gain_mismatch(
    db: sqlite3.Connection,
) -> None:
    """Regression: Dwarf 3 factory bias filenames carry a gain index that's
    not the photographic gain on lights. Adapter stores gain=NULL on those
    masters; the matcher must still find them for sessions at gain=60."""
    sid = _insert_session(db, gain=60, binning=1)
    bias = _insert_master(db, kind="bias", gain=None, binning=1, exptime=None,
                          path="/tmp/bias-factory.fits")
    out = match_session(db, sid)
    assert out["bias"]["master_id"] == bias
    assert out["bias"]["match_quality"] == "exact"


def test_flat_factory_matches_on_filter_and_bin_only(
    db: sqlite3.Connection,
) -> None:
    """Regression: factory flats have gain/exptime NULL. Match purely on
    (filter, binning, camera) so a session at gain=60 + Astro filter still
    finds the matching factory flat."""
    sid = _insert_session(db, filter_="Astro", gain=60, binning=1)
    flat = _insert_master(
        db, kind="flat", filter_="Astro", gain=None, binning=1,
        exptime=None, path="/tmp/flat-astro.fits",
    )
    out = match_session(db, sid)
    assert out["flat"]["master_id"] == flat
    assert out["flat"]["match_quality"] == "exact"


def test_flat_match_picks_most_recent(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, filter_="Astro", gain=60, binning=1)
    older = _insert_master(
        db, kind="flat", filter_="Astro", gain=60, binning=1, date_built="2025-09-01",
        path="/tmp/flat-older.fits",
    )
    newer = _insert_master(
        db, kind="flat", filter_="Astro", gain=60, binning=1, date_built="2025-10-15",
        path="/tmp/flat-newer.fits",
    )
    out = match_session(db, sid)
    assert out["flat"]["master_id"] == newer
    assert older is not None


def test_user_override_preserved(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, exptime=30.0, avg_temp=22.0)
    auto = _insert_master(db, kind="dark", exptime=30.0, ccd_temp=22.0)
    pinned = _insert_master(db, kind="dark", exptime=30.0, ccd_temp=18.0, stack_count=2)

    db.execute(
        """INSERT INTO calibration_matches
            (session_id, kind, master_id, match_quality, details, overridden, updated_at)
           VALUES (?, 'dark', ?, 'approx', ?, 1, 0)""",
        (sid, pinned, json.dumps({"reason": "user pick"})),
    )

    out = match_session(db, sid)
    assert out["dark"]["master_id"] == pinned
    assert out["dark"]["overridden"] is True
    assert auto is not None  # candidate exists but ignored


def test_persisted_match_round_trips(db: sqlite3.Connection) -> None:
    sid = _insert_session(db, exptime=30.0, avg_temp=22.0)
    chosen = _insert_master(db, kind="dark", exptime=30.0, ccd_temp=22.0)

    match_session(db, sid)
    row = db.execute(
        "SELECT master_id, match_quality, overridden FROM calibration_matches "
        "WHERE session_id = ? AND kind = 'dark'",
        (sid,),
    ).fetchone()
    assert row["master_id"] == chosen
    assert row["match_quality"] == "exact"
    assert row["overridden"] == 0
