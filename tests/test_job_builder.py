"""Job builder: catalog session + canned template -> runnable Job."""

from __future__ import annotations

from pathlib import Path

import pytest

import nodes.basic  # noqa: F401
from server.catalog.db import open_db
from server.job_builder import (
    CalibrationMissing,
    JobBuildError,
    SessionNotFound,
    TooFewFrames,
    build_from_session,
    session_lights_folder,
)
from server.models import CalibrationSpec
from server.templates import load_template


def _seed_session(
    conn,
    *,
    session_id: int = 1,
    target_id: int = 1,
    target_name: str = "M 33",
    folder: Path,
    n_frames: int = 3,
) -> None:
    """Insert a target/session/frames triplet pointing at `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n_frames):
        (folder / f"frame_{i}.fits").write_bytes(b"FAKE")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO targets (id, name) VALUES (?, ?)",
            (target_id, target_name),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
            (id, scope_id, session_key, target_id, instrument, exptime, gain, binning,
             frame_count, failed_count)
            VALUES (?, 'dwarf3', ?, ?, 'DWARFIII', 30.0, 60, 1, ?, 0)
            """,
            (session_id, f"key-{session_id}", target_id, n_frames),
        )
        for i in range(n_frames):
            cur = conn.execute(
                """
                INSERT INTO frames (path, image_type, instrument, exptime, gain, session_key)
                VALUES (?, 'LIGHT', 'DWARFIII', 30.0, 60, ?)
                """,
                (str(folder / f"frame_{i}.fits"), f"key-{session_id}"),
            )
            conn.execute(
                "INSERT INTO session_frames (session_id, frame_id) VALUES (?, ?)",
                (session_id, cur.lastrowid),
            )


def _seed_master_dark(conn, *, master_id: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MASTER")
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO masters
            (id, kind, source, instrument, exptime, gain, binning, ccd_temp,
             stack_count, path)
            VALUES (?, 'dark', 'factory', 'DWARFIII', 30.0, 60, 1, 28.0, 10, ?)
            """,
            (master_id, str(path)),
        )


def _set_calibration_match(conn, session_id: int, master_id: int) -> None:
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO calibration_matches
            (session_id, kind, master_id, match_quality)
            VALUES (?, 'dark', ?, 'exact')
            """,
            (session_id, master_id),
        )


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "catalog.sqlite"
    with open_db(db_path) as conn:
        yield conn


def test_session_lights_folder_returns_parent(db, tmp_path: Path) -> None:
    folder = tmp_path / "session_a"
    _seed_session(db, folder=folder)
    assert session_lights_folder(db, 1) == folder


def test_session_lights_folder_raises_when_empty(db) -> None:
    with pytest.raises(SessionNotFound):
        session_lights_folder(db, 99)


def test_session_lights_folder_raises_when_split_across_dirs(
    db, tmp_path: Path
) -> None:
    # Manually plant frames under two different parents so the heuristic bails.
    folder1 = tmp_path / "split_a"
    folder1.mkdir()
    folder2 = tmp_path / "split_b"
    folder2.mkdir()
    (folder1 / "f1.fits").write_bytes(b"")
    (folder2 / "f2.fits").write_bytes(b"")
    with db:
        db.execute(
            "INSERT INTO targets (id, name) VALUES (1, 'X')"
        )
        db.execute(
            """INSERT INTO sessions (id, scope_id, session_key, target_id, frame_count)
               VALUES (1, 'dwarf3', 'k1', 1, 2)"""
        )
        for p in [folder1 / "f1.fits", folder2 / "f2.fits"]:
            cur = db.execute(
                "INSERT INTO frames (path, image_type) VALUES (?, 'LIGHT')",
                (str(p),),
            )
            db.execute(
                "INSERT INTO session_frames (session_id, frame_id) VALUES (1, ?)",
                (cur.lastrowid,),
            )
    with pytest.raises(JobBuildError, match="spans"):
        session_lights_folder(db, 1)


def test_build_from_session_auto_uses_matched_master(db, tmp_path: Path) -> None:
    _seed_session(db, folder=tmp_path / "sess")
    _seed_master_dark(db, master_id=1, path=tmp_path / "masters" / "dark.fit")
    _set_calibration_match(db, session_id=1, master_id=1)

    template = load_template("calibrate_register_stack")
    job = build_from_session(db, 1, template)

    assert job.template_id == "calibrate_register_stack"
    assert "convert.lights" in job.inputs
    assert job.inputs["convert.lights"].path == (tmp_path / "sess").resolve() or \
           job.inputs["convert.lights"].path == tmp_path / "sess"
    assert "calibrate.dark" in job.inputs
    assert job.inputs["calibrate.dark"].path == tmp_path / "masters" / "dark.fit"


def test_build_from_session_fails_when_no_master_matched(db, tmp_path: Path) -> None:
    _seed_session(db, folder=tmp_path / "sess")
    template = load_template("calibrate_register_stack")
    with pytest.raises(CalibrationMissing, match="no matched master dark"):
        build_from_session(db, 1, template)


def test_build_from_session_explicit_master_id(db, tmp_path: Path) -> None:
    _seed_session(db, folder=tmp_path / "sess")
    _seed_master_dark(db, master_id=42, path=tmp_path / "masters" / "explicit.fit")

    template = load_template("calibrate_register_stack")
    job = build_from_session(
        db, 1, template,
        calibration=CalibrationSpec(mode="explicit", master_ids={"dark": 42}),
    )
    assert job.inputs["calibrate.dark"].path == tmp_path / "masters" / "explicit.fit"


def test_build_rejects_single_frame_session(db, tmp_path: Path) -> None:
    _seed_session(db, folder=tmp_path / "tiny", n_frames=1)
    template = load_template("calibrate_register_stack")
    with pytest.raises(TooFewFrames, match="only 1 light frame"):
        build_from_session(db, 1, template)


def test_build_from_session_explicit_master_missing_404s(db, tmp_path: Path) -> None:
    _seed_session(db, folder=tmp_path / "sess")
    template = load_template("calibrate_register_stack")
    with pytest.raises(CalibrationMissing, match="not found"):
        build_from_session(
            db, 1, template,
            calibration=CalibrationSpec(mode="explicit", master_ids={"dark": 999}),
        )
