"""Scope profile loader + matcher integration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from server import profiles
from server.catalog.matching import match_session


def _seed_session(conn: sqlite3.Connection, *, scope_id: str) -> int:
    """Insert a minimal session and one light frame; return session id."""
    target_cur = conn.execute(
        "INSERT INTO targets (name) VALUES (?)", ("M 33",)
    )
    target_id = target_cur.lastrowid
    cur = conn.execute(
        """
        INSERT INTO sessions
        (scope_id, target_id, instrument, exptime, gain, binning,
         frame_count, failed_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (scope_id, target_id, "DWARFIII", 30.0, 60, 1, 10, 0),
    )
    sid = int(cur.lastrowid or -1)
    frame_cur = conn.execute(
        """
        INSERT INTO frames (path, image_type, scope_id, instrument,
                            exptime, gain, binning, ccd_temp)
        VALUES (?, 'LIGHT', ?, 'DWARFIII', 30.0, 60, 1, 22.0)
        """,
        (f"/tmp/seed-{sid}.fits", scope_id),
    )
    conn.execute(
        "INSERT INTO session_frames (session_id, frame_id) VALUES (?, ?)",
        (sid, frame_cur.lastrowid),
    )
    return sid


@pytest.fixture
def conn(tmp_path: Path):
    from server.catalog.db import connect

    c = connect(tmp_path / "cat.sqlite")
    yield c
    c.close()


# ---------- loader -------------------------------------------------------


def test_loader_finds_dwarf3_profile() -> None:
    profile = profiles.get("dwarf3")
    assert profile.id == "dwarf3"
    assert profile.display_name == "DWARF 3"
    assert profile.calibration.skip_match is False


def test_loader_finds_seestar_profile() -> None:
    profile = profiles.get("seestar")
    assert profile.id == "seestar"
    assert profile.calibration.skip_match is True
    assert profile.calibration.skip_reason
    assert "Seestar" in profile.calibration.skip_reason


def test_loader_finds_nina_and_asiair() -> None:
    """NINA and ASIAIR ship as stub profiles in v1 — present, no overrides."""
    assert profiles.get("nina").id == "nina"
    assert profiles.get("asiair").id == "asiair"


def test_unknown_scope_returns_default_profile() -> None:
    """An unrecognized scope_id falls back to a non-throwing default with
    skip_match=False, so the matcher runs normally."""
    profile = profiles.get("not-a-real-scope")
    assert profile.calibration.skip_match is False


def test_none_scope_id_returns_default() -> None:
    assert profiles.get(None).calibration.skip_match is False


# ---------- matcher integration ------------------------------------------


def test_seestar_session_gets_not_needed_for_all_kinds(conn) -> None:
    """A Seestar session must not have the matcher try to find masters; all
    three kinds get match_quality='not_needed' with a human-readable reason."""
    sid = _seed_session(conn, scope_id="seestar")
    result = match_session(conn, sid)
    for kind in ("dark", "flat", "bias"):
        assert result[kind]["match_quality"] == "not_needed", kind
        assert result[kind]["master_id"] is None, kind
        assert result[kind]["details"]["reason"]


def test_dwarf3_session_runs_normal_matcher(conn) -> None:
    """A Dwarf 3 session goes through the normal matcher path. With no
    masters seeded the result is 'none' for every kind — which is the
    expected 'no match found' state, distinct from 'not_needed'."""
    sid = _seed_session(conn, scope_id="dwarf3")
    result = match_session(conn, sid)
    for kind in ("dark", "flat", "bias"):
        assert result[kind]["match_quality"] == "none", kind
        assert result[kind]["master_id"] is None, kind


def test_seestar_match_is_persisted(conn) -> None:
    """The 'not_needed' result must hit calibration_matches so the UI
    reads it back via the normal session-detail query."""
    sid = _seed_session(conn, scope_id="seestar")
    match_session(conn, sid)
    rows = conn.execute(
        "SELECT kind, match_quality FROM calibration_matches WHERE session_id = ?",
        (sid,),
    ).fetchall()
    assert {r["kind"] for r in rows} == {"dark", "flat", "bias"}
    for r in rows:
        assert r["match_quality"] == "not_needed"
