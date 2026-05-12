"""End-to-end scanner tests against a synthetic Dwarf 3 tree."""

from __future__ import annotations

from pathlib import Path

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.catalog.db import open_db
from server.catalog.scanner import scan

from ._fits_fixtures import DEFAULT_DARK_HEADER, DEFAULT_LIGHT_HEADER, write_fits


def _build_dwarf_tree(root: Path) -> None:
    """Two light folders (one M 33 ok session, one with a failed sub) and a dark folder."""
    m33 = root / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    write_fits(
        m33 / "M 33_30s60_Astro_20251021-221929504_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )
    write_fits(
        m33 / "M 33_30s60_Astro_20251021-222129500_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )
    write_fits(
        m33 / "failed_M 33_30s60_Astro_20251021-222229500_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )

    ngc = root / "DWARF_RAW_TELE_NGC 7380_EXP_15_GAIN_60_2025-10-18-22-15-19-903"
    write_fits(
        ngc / "NGC 7380_15s60_Astro_20251018-221600000_22C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "NGC 7380", "EXPTIME": 15.0},
    )

    dark = root / "DWARF_DARK" / "tele_exp_30_gain_60_bin_1_2025-10-21-00-37-45-393"
    write_fits(
        dark / "raw_30s_60_0000_20251021-003814624_22C.fits",
        headers=DEFAULT_DARK_HEADER,
    )
    write_fits(
        dark / "raw_30s_60_0001_20251021-003844601_22C.fits",
        headers=DEFAULT_DARK_HEADER,
    )


def test_scan_inserts_frames_and_sessions(tmp_path: Path, astrolab_home: Path) -> None:
    captures = tmp_path / "captures"
    _build_dwarf_tree(captures)

    stats = scan(captures)
    assert stats.discovered == 6
    assert stats.inserted == 6
    assert stats.failed == 0

    with open_db() as conn:
        frames = conn.execute("SELECT * FROM frames ORDER BY path").fetchall()
        assert len(frames) == 6
        lights = [r for r in frames if r["image_type"] == "LIGHT"]
        darks = [r for r in frames if r["image_type"] == "DARK"]
        assert len(lights) == 4
        assert len(darks) == 2

        failed = [r for r in lights if r["quality"] == "failed"]
        assert len(failed) == 1
        assert failed[0]["object"] == "M 33"

        targets = {r["name"] for r in conn.execute("SELECT name FROM targets").fetchall()}
        assert targets == {"M 33", "NGC 7380"}

        sessions = conn.execute(
            """
            SELECT s.*, t.name AS target_name FROM sessions s
            JOIN targets t ON t.id = s.target_id
            ORDER BY t.name
            """
        ).fetchall()
        # Two light sessions; darks have null target so produce no session row.
        assert len(sessions) == 2
        m33_session = next(s for s in sessions if s["target_name"] == "M 33")
        assert m33_session["frame_count"] == 3
        assert m33_session["failed_count"] == 1
        # Dwarf 3 writes "Astro" on its IR-cut filter; canonicalization at
        # scanner write time collapses that to "None" (no narrowband filter)
        # so matchers can join across scopes (see filter_aliases.py).
        assert m33_session["filter"] == "None"
        assert m33_session["exptime"] == 30.0


def test_scan_is_incremental(tmp_path: Path, astrolab_home: Path) -> None:
    captures = tmp_path / "captures"
    _build_dwarf_tree(captures)

    first = scan(captures)
    assert first.inserted == 6
    assert first.skipped_unchanged == 0

    second = scan(captures)
    assert second.inserted == 0
    assert second.updated == 0
    assert second.skipped_unchanged == 6


def test_scan_picks_up_new_files(tmp_path: Path, astrolab_home: Path) -> None:
    captures = tmp_path / "captures"
    _build_dwarf_tree(captures)
    scan(captures)

    extra = captures / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    write_fits(
        extra / "M 33_30s60_Astro_20251021-222329500_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )

    stats = scan(captures)
    assert stats.inserted == 1
    assert stats.skipped_unchanged == 6

    with open_db() as conn:
        m33_session = conn.execute(
            """
            SELECT s.frame_count FROM sessions s
            JOIN targets t ON t.id = s.target_id
            WHERE t.name = 'M 33'
            """
        ).fetchone()
        assert m33_session["frame_count"] == 4


def test_scan_handles_empty_root(tmp_path: Path, astrolab_home: Path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    stats = scan(empty)
    assert stats.discovered == 0
    assert stats.inserted == 0


def test_scan_handles_missing_root(tmp_path: Path, astrolab_home: Path) -> None:
    missing = tmp_path / "does-not-exist"
    stats = scan(missing)
    assert stats.discovered == 0


def test_scan_incremental_refresh_populates_sessions_early(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """Sessions must exist in the DB before the scan finishes.

    The scanner runs an incremental _refresh_sessions every 50 frames.
    We build 51 light frames so the threshold fires after frame 50 is
    ingested. The progress callback for frame 51 then sees the sessions
    row already in the DB, confirming they populated mid-scan.
    """
    captures = tmp_path / "captures"

    # Build 51 light frames: batch of 50 triggers the refresh, frame 51's
    # progress callback verifies the refresh ran before the scan ends.
    session_dir = captures / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    for i in range(51):
        write_fits(
            session_dir / f"M 33_30s60_Astro_20251021-{i:06d}_24C.fits",
            headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
        )

    sessions_seen_mid_scan: list[int] = []

    def _progress(seen: int, _total: int, path: str) -> None:
        # Frame 51's callback fires after frames 1-50 are ingested and the
        # incremental refresh has run (refresh triggers when _frames_since_refresh
        # hits 50, which happens after the 50th ingest, before this callback).
        if seen == 51:
            with open_db() as conn:
                count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                sessions_seen_mid_scan.append(count)

    scan(captures, progress=_progress)

    # The incremental refresh must have fired and written at least one session.
    assert len(sessions_seen_mid_scan) > 0, "progress callback never reached frame 51"
    assert sessions_seen_mid_scan[0] >= 1, (
        f"expected at least 1 session mid-scan, got {sessions_seen_mid_scan[0]}"
    )
