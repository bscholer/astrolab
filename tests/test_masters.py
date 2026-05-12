"""Master discovery + ingest tests."""

from __future__ import annotations

from pathlib import Path

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.catalog.db import open_db
from server.catalog.scanner import scan

from ._fits_fixtures import DEFAULT_DARK_HEADER, DEFAULT_LIGHT_HEADER, write_fits


def _build_tree_with_cali(root: Path) -> None:
    """A light session, raw darks, and CALI_FRAME masters all together."""
    light = root / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    write_fits(
        light / "M 33_30s60_Astro_20251021-221929504_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )
    raw_dark = root / "DWARF_DARK" / "tele_exp_30_gain_60_bin_1_2025-10-21-00-37-45-393"
    write_fits(
        raw_dark / "raw_30s_60_0000_20251021-003814624_22C.fits",
        headers=DEFAULT_DARK_HEADER,
    )

    cali_dark = root / "CALI_FRAME" / "dark" / "cam_0"
    write_fits(cali_dark / "dark_exp_30.000000_gain_60_bin_1_22C_stack_10.fits")
    write_fits(cali_dark / "dark_exp_15.000000_gain_60_bin_1_28C_stack_10.fits")
    write_fits(cali_dark / "dark_exp_30.000000_gain_60_bin_1_25C_stack_5.fits")

    cali_bias = root / "CALI_FRAME" / "bias" / "cam_0"
    write_fits(cali_bias / "bias_gain_2_bin_1.fits")

    cali_flat = root / "CALI_FRAME" / "flat" / "cam_0"
    write_fits(cali_flat / "flat_gain_2_bin_1_ir_1.fits")  # ir_1 = Astro


def test_scan_inserts_masters(tmp_path: Path, astrolab_home: Path) -> None:
    captures = tmp_path / "captures"
    _build_tree_with_cali(captures)

    stats = scan(captures)
    assert stats.masters_inserted == 5
    assert stats.masters_skipped == 0

    with open_db() as conn:
        rows = conn.execute(
            "SELECT kind, exptime, gain, binning, ccd_temp, stack_count, camera, source "
            "FROM masters ORDER BY kind, exptime, ccd_temp"
        ).fetchall()
        assert [r["kind"] for r in rows] == ["bias", "dark", "dark", "dark", "flat"]
        for r in rows:
            assert r["camera"] == "TELE"
            assert r["source"] == "factory"


def test_master_ingest_idempotent(tmp_path: Path, astrolab_home: Path) -> None:
    captures = tmp_path / "captures"
    _build_tree_with_cali(captures)
    scan(captures)
    second = scan(captures)
    assert second.masters_inserted == 0
    assert second.masters_skipped == 5


def test_orphan_master_removed(tmp_path: Path, astrolab_home: Path) -> None:
    captures = tmp_path / "captures"
    _build_tree_with_cali(captures)
    scan(captures)

    victim = (
        captures
        / "CALI_FRAME"
        / "dark"
        / "cam_0"
        / "dark_exp_30.000000_gain_60_bin_1_25C_stack_5.fits"
    )
    victim.unlink()

    stats = scan(captures)
    assert stats.masters_removed == 1
    with open_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM masters").fetchone()["n"]
        assert n == 4
