"""Dwarf 3 mosaic ingestion.

Mosaic captures nest one folder per panel under an outer MOSAIC_<target>
session folder. With per-file scope detection the universal walker
descends into both layers and treats every FITS as its own frame; the
panel suffix ``(N)`` in each frame's OBJECT header is stripped by
``normalize_target`` so all panels land under one target row. Time-gap
clustering then naturally groups the panels into one session because
they're captured back-to-back.
"""

from __future__ import annotations

from pathlib import Path

from server.catalog.db import open_db
from server.catalog.fits_reader import normalize_target
from server.catalog.scanner import scan

from ._fits_fixtures import DEFAULT_LIGHT_HEADER, write_fits


def test_normalize_strips_panel_suffix() -> None:
    assert normalize_target("M 31(1)") == "M 31"
    assert normalize_target("M 31(2)") == "M 31"
    assert normalize_target("NGC 7380(10)") == "NGC 7380"
    # Plain names unaffected.
    assert normalize_target("M 33") == "M 33"
    assert normalize_target("HD 173764") == "HD 173764"


def test_mosaic_full_scan_collapses_to_one_target_one_session(
    tmp_path: Path, astrolab_home: Path
) -> None:
    captures = tmp_path / "captures"
    outer = (
        captures
        / "DWARF_RAW_TELE_MOSAIC_M 31_EXP_15_GAIN_60_2025-10-21-20-55-45-308"
    )
    panel1 = outer / "DWARF_RAW_TELE_M 31(1)_EXP_15_GAIN_60_2025-10-21-20-55-45-311"
    panel2 = outer / "DWARF_RAW_TELE_M 31(2)_EXP_15_GAIN_60_2025-10-21-20-55-45-312"
    write_fits(
        panel1 / "M 31(1)_15s60_Astro_20251021-205739610_26C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 31(1)"},
    )
    write_fits(
        panel2 / "M 31(2)_15s60_Astro_20251021-205740111_26C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 31(2)"},
    )

    stats = scan(captures)
    assert stats.inserted == 2

    with open_db() as conn:
        targets = [r["name"] for r in conn.execute("SELECT name FROM targets")]
        assert targets == ["M 31"], "panel suffixes collapse to one target"
        sessions = conn.execute(
            "SELECT frame_count FROM sessions"
        ).fetchall()
        assert len(sessions) == 1
        assert sessions[0]["frame_count"] == 2
