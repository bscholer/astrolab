"""Job builder: catalog session + canned template -> runnable Job."""

from __future__ import annotations

from pathlib import Path

import pytest

import nodes.basic  # noqa: F401
from server.catalog.db import open_db
from server.job_builder import (
    CalibrationMissing,
    IncompatibleSessions,
    JobBuildError,
    SessionNotFound,
    TooFewFrames,
    build_from_session,
    build_from_sessions,
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
    instrument: str = "DWARFIII",
    exptime: float = 30.0,
    gain: int = 60,
    binning: int = 1,
    filter_name: str | None = None,
    camera: str | None = None,
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
            (id, scope_id, target_id, instrument, camera, filter,
             exptime, gain, binning, frame_count, failed_count)
            VALUES (?, 'dwarf3', ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                session_id,
                target_id,
                instrument,
                camera,
                filter_name,
                exptime,
                gain,
                binning,
                n_frames,
            ),
        )
        for i in range(n_frames):
            cur = conn.execute(
                """
                INSERT INTO frames (path, image_type, instrument, exptime, gain)
                VALUES (?, 'LIGHT', ?, ?, ?)
                """,
                (
                    str(folder / f"frame_{i}.fits"),
                    instrument,
                    exptime,
                    gain,
                ),
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
            """INSERT INTO sessions (id, scope_id, target_id, frame_count)
               VALUES (1, 'dwarf3', 1, 2)"""
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
    # calibrate.dark is a MASTER_FITS_LIST port; auto-matched bundles
    # produce a list of matching darks (one per (exptime, gain, temp)
    # bin). A single-session homogeneous bundle ends up with one entry.
    dark_input = job.inputs["calibrate.dark"]
    assert isinstance(dark_input, list)
    assert len(dark_input) == 1
    assert dark_input[0].path == tmp_path / "masters" / "dark.fit"


def test_build_from_session_skips_unmatched_optional_master(db, tmp_path: Path) -> None:
    """With dark declared optional on calibrate, auto mode silently builds a
    job that simply omits calibrate.dark when nothing matches. Calibration is
    suboptimal but the pipeline still runs end-to-end — surfacing this as a
    hard error blocked legitimate sessions whose gain/temp don't match an
    indexed master."""
    _seed_session(db, folder=tmp_path / "sess")
    template = load_template("calibrate_register_stack")
    job = build_from_session(db, 1, template)
    assert "calibrate.dark" not in job.inputs
    assert "convert.lights" in job.inputs


def test_build_from_session_skips_dark_in_none_mode(db, tmp_path: Path) -> None:
    """mode='none' explicitly skips master wiring even when one is matched.
    Used by the 'just process the lights' affordance."""
    _seed_session(db, folder=tmp_path / "sess")
    _seed_master_dark(db, master_id=1, path=tmp_path / "masters" / "dark.fit")
    template = load_template("calibrate_register_stack")
    job = build_from_session(
        db, 1, template, calibration=CalibrationSpec(mode="none")
    )
    assert "calibrate.dark" not in job.inputs


def test_build_from_session_explicit_master_id(db, tmp_path: Path) -> None:
    _seed_session(db, folder=tmp_path / "sess")
    _seed_master_dark(db, master_id=42, path=tmp_path / "masters" / "explicit.fit")

    template = load_template("calibrate_register_stack")
    job = build_from_session(
        db, 1, template,
        calibration=CalibrationSpec(mode="explicit", master_ids={"dark": 42}),
    )
    dark_input = job.inputs["calibrate.dark"]
    assert isinstance(dark_input, list)
    assert dark_input[0].path == tmp_path / "masters" / "explicit.fit"


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


# ---------------------------------------------------------------------------
# Multi-session
# ---------------------------------------------------------------------------


def test_build_from_sessions_n1_matches_single(db, tmp_path: Path) -> None:
    """N=1 must hit the same lights path as build_from_session — multi-session
    is a strict superset, not a different code path for the simple case."""
    _seed_session(db, folder=tmp_path / "sess")
    template = load_template("calibrate_register_stack")
    job_single = build_from_session(db, 1, template)
    job_multi = build_from_sessions(db, [1], template)
    assert (
        job_single.inputs["convert.lights"].path
        == job_multi.inputs["convert.lights"].path
    )


def test_build_from_sessions_stages_symlinks(db, tmp_path: Path) -> None:
    """Two compatible sessions get bundled into one staging dir of symlinks."""
    _seed_session(db, session_id=1, folder=tmp_path / "s1", n_frames=3)
    _seed_session(db, session_id=2, folder=tmp_path / "s2", n_frames=3)
    template = load_template("calibrate_register_stack")
    job = build_from_sessions(db, [1, 2], template)

    stage = job.inputs["convert.lights"].path
    assert stage.is_dir()
    links = sorted(p.name for p in stage.iterdir() if p.is_symlink())
    # Both sessions' frames land in the staging dir, prefixed by session id
    # to avoid collisions on identical capture filenames.
    assert any(name.startswith("s1__") for name in links)
    assert any(name.startswith("s2__") for name in links)
    assert len(links) == 6


def test_build_from_sessions_deterministic_path(db, tmp_path: Path) -> None:
    """Same bundle in any order must hit the same staging dir so re-runs
    re-use the convert_lights cache entry."""
    _seed_session(db, session_id=1, folder=tmp_path / "s1")
    _seed_session(db, session_id=2, folder=tmp_path / "s2")
    template = load_template("calibrate_register_stack")
    a = build_from_sessions(db, [1, 2], template).inputs["convert.lights"].path
    b = build_from_sessions(db, [2, 1], template).inputs["convert.lights"].path
    assert a == b


def test_build_from_sessions_rejects_mismatched_gain(db, tmp_path: Path) -> None:
    _seed_session(db, session_id=1, folder=tmp_path / "s1", gain=60)
    _seed_session(db, session_id=2, folder=tmp_path / "s2", gain=80)
    template = load_template("calibrate_register_stack")
    with pytest.raises(IncompatibleSessions, match="gain"):
        build_from_sessions(db, [1, 2], template)


def test_build_from_sessions_accepts_differing_target_ids(db, tmp_path: Path) -> None:
    """build_from_sessions no longer rejects on raw target_id. Target
    identity is enforced at the API layer via canonical-group matching
    (api._assert_sessions_share_target) so manually-retargeted sessions
    work; the builder's job is the stacker-relevant compat tuple only.

    For coverage of the API-level same-target gate (which DOES still
    reject two truly different targets), see tests under tests/test_api*
    that exercise the POST /from_sessions and PATCH /sessions endpoints.
    """
    _seed_session(
        db, session_id=1, folder=tmp_path / "s1",
        target_id=1, target_name="M 33",
    )
    _seed_session(
        db, session_id=2, folder=tmp_path / "s2",
        target_id=2, target_name="M 31",
    )
    template = load_template("calibrate_register_stack")
    # Should not raise; differing target_ids no longer trip the builder.
    build_from_sessions(db, [1, 2], template)


def test_build_from_sessions_uses_first_sessions_master(db, tmp_path: Path) -> None:
    """A bundle of two compatible sessions (same exptime/gain/binning)
    produces a one-entry dark list when only one master in the library
    matches that exposure/gain bucket. The bundle matcher consults the
    masters table directly, so per-session calibration_matches rows are
    not required."""
    _seed_session(db, session_id=1, folder=tmp_path / "s1")
    _seed_session(db, session_id=2, folder=tmp_path / "s2")
    _seed_master_dark(db, master_id=7, path=tmp_path / "masters" / "d.fit")
    _set_calibration_match(db, session_id=1, master_id=7)
    template = load_template("calibrate_register_stack")
    job = build_from_sessions(db, [2, 1], template)
    dark_input = job.inputs["calibrate.dark"]
    assert isinstance(dark_input, list)
    paths = [r.path for r in dark_input]
    assert tmp_path / "masters" / "d.fit" in paths


def test_build_from_sessions_rejects_too_few_total_frames(db, tmp_path: Path) -> None:
    _seed_session(db, session_id=1, folder=tmp_path / "s1", n_frames=1)
    _seed_session(db, session_id=2, folder=tmp_path / "s2", n_frames=1)
    template = load_template("calibrate_register_stack")
    with pytest.raises(TooFewFrames):
        build_from_sessions(db, [1, 2], template)


def test_build_from_sessions_empty_input_raises(db) -> None:
    template = load_template("calibrate_register_stack")
    with pytest.raises(JobBuildError, match="at least one"):
        build_from_sessions(db, [], template)
