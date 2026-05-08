"""Dwarf 3 mosaic ingestion.

Mosaic captures nest one folder per panel under the outer MOSAIC_<target>
session folder. The adapter should treat the outer folder as the session,
strip the MOSAIC_ prefix from the target name, descend into the inner panel
folders, and emit all frames under one session_key. The OBJECT header on
panel frames carries an '(N)' suffix that the FITS reader strips when
normalizing the target name.
"""

from __future__ import annotations

from pathlib import Path

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.catalog.adapters.dwarf3 import DwarfThreeAdapter
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


def test_adapter_descends_into_mosaic_panels(tmp_path: Path) -> None:
    outer = tmp_path / "DWARF_RAW_TELE_MOSAIC_M 31_EXP_15_GAIN_60_2025-10-21-20-55-45-308"
    panel1 = outer / "DWARF_RAW_TELE_M 31(1)_EXP_15_GAIN_60_2025-10-21-20-55-45-311"
    panel2 = outer / "DWARF_RAW_TELE_M 31(2)_EXP_15_GAIN_60_2025-10-21-20-55-45-312"
    panel1.mkdir(parents=True)
    panel2.mkdir(parents=True)
    (panel1 / "M 31(1)_15s60_Astro_20251021-205739610_26C.fits").write_bytes(b"")
    (panel1 / "stacked-16_M 31(1)_15s60_Astro_20251021-205725378.fits").write_bytes(b"")
    (panel2 / "M 31(2)_15s60_Astro_20251021-205740111_26C.fits").write_bytes(b"")
    (panel2 / "failed_M 31(2)_15s60_Astro_20251021-205800000_26C.fits").write_bytes(b"")

    found = list(DwarfThreeAdapter().discover(tmp_path))
    assert len(found) == 3, "should yield 2 ok lights + 1 failed; skip stacked-"

    session_keys = {d.session_key for d in found}
    assert session_keys == {f"dwarf3:{outer.name}"}, "all panels share outer session key"

    for d in found:
        assert d.image_type == "LIGHT"
        assert d.session_hints is not None
        assert d.session_hints["is_mosaic"] is True
        assert d.session_hints["target_from_path"] == "M 31"


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

    stats = scan(captures, scope_id="dwarf3")
    assert stats.inserted == 2

    with open_db() as conn:
        targets = [r["name"] for r in conn.execute("SELECT name FROM targets")]
        assert targets == ["M 31"], "panel suffixes collapse to one target"
        sessions = conn.execute(
            "SELECT session_key, frame_count FROM sessions"
        ).fetchall()
        assert len(sessions) == 1
        assert sessions[0]["frame_count"] == 2
