"""FITS header reader tests.

Uses astropy to write a tiny synthetic FITS, then reads it back and asserts
the canonical headers come through.
"""

from __future__ import annotations

from pathlib import Path

from server.catalog.fits_reader import normalize_target, read_primary_header

from ._fits_fixtures import DEFAULT_LIGHT_HEADER, write_fits


def test_reads_known_keys(tmp_path: Path) -> None:
    fp = tmp_path / "light.fits"
    write_fits(fp, headers=DEFAULT_LIGHT_HEADER)

    out = read_primary_header(fp)
    assert out["OBJECT"] == "M 33"
    assert out["EXPTIME"] == 30.0
    assert out["GAIN"] == 60
    assert out["FILTER"] == "Astro"
    assert out["CAMERA"] == "TELE"
    assert out["INSTRUME"] == "DWARFIII"


def test_string_values_stripped(tmp_path: Path) -> None:
    fp = tmp_path / "padded.fits"
    write_fits(fp, headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33    "})
    out = read_primary_header(fp)
    assert out["OBJECT"] == "M 33"


def test_missing_keys_absent(tmp_path: Path) -> None:
    fp = tmp_path / "minimal.fits"
    write_fits(fp, headers={"OBJECT": "Test"})
    out = read_primary_header(fp)
    assert "OBJECT" in out
    assert "FILTER" not in out
    assert "GAIN" not in out


def test_normalize_target_collapses_whitespace() -> None:
    assert normalize_target("M  33") == "M 33"
    assert normalize_target("  NGC   7380  ") == "NGC 7380"
    assert normalize_target("M33") == "M33"
