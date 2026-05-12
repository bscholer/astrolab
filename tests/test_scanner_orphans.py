"""Orphan-cleanup behavior of the scanner."""

from __future__ import annotations

from pathlib import Path

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.catalog.db import open_db
from server.catalog.scanner import scan

from ._fits_fixtures import DEFAULT_LIGHT_HEADER, write_fits


def _build_minimal_tree(root: Path) -> Path:
    folder = root / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    write_fits(
        folder / "M 33_30s60_Astro_20251021-221929504_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )
    write_fits(
        folder / "M 33_30s60_Astro_20251021-222129500_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )
    return folder


def test_removed_files_are_orphaned(tmp_path: Path, astrolab_home: Path) -> None:
    captures = tmp_path / "captures"
    folder = _build_minimal_tree(captures)

    first = scan(captures)
    assert first.inserted == 2

    # Delete one frame from disk; rescan should remove its row.
    victim = folder / "M 33_30s60_Astro_20251021-222129500_24C.fits"
    victim.unlink()

    second = scan(captures)
    assert second.removed == 1
    assert second.skipped_unchanged == 1

    with open_db() as conn:
        remaining = conn.execute("SELECT COUNT(*) AS n FROM frames").fetchone()["n"]
        assert remaining == 1


def test_emptied_session_is_dropped(tmp_path: Path, astrolab_home: Path) -> None:
    captures = tmp_path / "captures"
    folder = _build_minimal_tree(captures)
    scan(captures)

    # Remove the entire session folder; rescan should clean up sessions and targets.
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()

    stats = scan(captures)
    assert stats.removed == 2

    with open_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM frames").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"] == 0


def test_orphan_cleanup_scoped_to_root(tmp_path: Path, astrolab_home: Path) -> None:
    """A scan of one root must not delete frames belonging to another root."""
    root_a = tmp_path / "captures-a"
    root_b = tmp_path / "captures-b"
    _build_minimal_tree(root_a)
    folder_b = root_b / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-22-01-04-55-033"
    write_fits(
        folder_b / "M 33_30s60_Astro_20251022-010500000_22C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )

    scan(root_a)
    scan(root_b)

    with open_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM frames").fetchone()["n"] == 3

    # Re-scan root_a only; root_b's frame must survive.
    scan(root_a)
    with open_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM frames").fetchone()["n"] == 3
