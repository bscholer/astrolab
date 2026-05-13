"""Tests for the Dwarf 3 scope-specific helpers.

Only two cases still need scope-specific code on the Dwarf 3 ingest path:

- Factory calibration masters under ``CALI_FRAME/`` (almost no FITS
  headers; everything's in the filename).
- User dark frames under ``DWARF_DARK/`` where the FITS header carries
  stale ``OBJECT`` / ``RA`` / ``DEC`` from the previous light capture and
  occasionally misses ``EXPTIME`` / ``GAIN`` / ``DATE-OBS``.

Lights and the universal classifier itself are covered in test_classify.py
and test_scanner.py.
"""

from __future__ import annotations

from pathlib import Path

from server.catalog.adapters.dwarf3 import (
    enrich_dark_header,
    walk_factory_masters,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


# ---------- factory masters ----------------------------------------------


def test_walks_dark_master(tmp_path: Path) -> None:
    f = tmp_path / "CALI_FRAME" / "dark" / "cam_0"
    _touch(f / "dark_exp_30.000000_gain_60_bin_1_22C_stack_10.fits")
    # An off-spec filename should be ignored, not parsed as garbage.
    _touch(f / "stray_extra_file.fits")

    found = list(walk_factory_masters(tmp_path))
    assert len(found) == 1
    dark = found[0]
    assert dark.kind == "dark"
    assert dark.camera == "TELE"
    assert dark.exptime == 30.0
    assert dark.gain == 60
    assert dark.binning == 1
    assert dark.ccd_temp == 22.0
    assert dark.stack_count == 10
    assert dark.source == "factory"
    assert dark.instrument == "DWARFIII"


def test_walks_flat_master_with_filter_from_ir_index(tmp_path: Path) -> None:
    """Factory flats encode ``ir_N`` in the filename and the adapter maps
    it back to the same FILTER string that Dwarf 3 lights carry in their
    FITS headers (``Astro`` / ``VIS`` / ``Duo``). The scanner runs the
    final write through ``filter_aliases.canonicalize``; the adapter
    itself just emits source-faithful strings."""
    f = tmp_path / "CALI_FRAME" / "flat" / "cam_0"
    _touch(f / "flat_gain_2_bin_1_ir_1.fits")  # ir_1 -> "Astro"
    _touch(f / "flat_gain_2_bin_1_ir_2.fits")  # ir_2 -> "Duo"

    found = sorted(walk_factory_masters(tmp_path), key=lambda d: d.filter or "")
    assert [f.filter for f in found] == ["Astro", "Duo"]
    for m in found:
        # Factory flats carry no photographic gain, no exposure, no temp.
        assert m.gain is None
        assert m.exptime is None
        assert m.ccd_temp is None


def test_walks_bias_master_binning_only(tmp_path: Path) -> None:
    """Factory bias is binning-only — no exposure, no filter, no temp."""
    f = tmp_path / "CALI_FRAME" / "bias" / "cam_1"
    _touch(f / "bias_gain_2_bin_1.fits")

    found = list(walk_factory_masters(tmp_path))
    assert len(found) == 1
    bias = found[0]
    assert bias.kind == "bias"
    assert bias.camera == "WIDE"
    assert bias.binning == 1
    assert bias.gain is None
    assert bias.filter is None
    assert bias.exptime is None
    assert bias.ccd_temp is None


def test_walk_skips_non_cali_dirs(tmp_path: Path) -> None:
    """Unknown subdirs under CALI_FRAME are ignored, not crashed on."""
    (tmp_path / "CALI_FRAME" / "weird").mkdir(parents=True)
    (tmp_path / "CALI_FRAME" / "dark" / "cam_2").mkdir(parents=True)
    assert list(walk_factory_masters(tmp_path)) == []


def test_walk_missing_root_is_empty(tmp_path: Path) -> None:
    assert list(walk_factory_masters(tmp_path / "does-not-exist")) == []


# ---------- dark header enrichment ---------------------------------------


def test_enrich_dark_drops_stale_object_ra_dec() -> None:
    """Dwarf 3 user darks ship with OBJECT / RA / DEC carried over from
    the previous light capture. Drop them so they don't create spurious
    target rows or pollute sky-match."""
    header = {
        "OBJECT": "M 31",
        "RA": 10.0,
        "DEC": 41.0,
        "FILTER": "Astro",
        "EXPTIME": 60.0,
        "GAIN": 60,
        "DATE-OBS": "2025-10-20T03:23:10.186",
    }
    out = enrich_dark_header(header, Path("/x/raw_60s_60_0002_20251020-032310186_20C.fits"))
    for key in ("OBJECT", "RA", "DEC", "FILTER"):
        assert key not in out
    # Headers the firmware got right are preserved.
    assert out["EXPTIME"] == 60.0
    assert out["GAIN"] == 60


def test_enrich_dark_fills_missing_keys_from_filename() -> None:
    """When the firmware leaves EXPTIME / GAIN / DATE-OBS / TEMP blank,
    the filename is authoritative."""
    header: dict[str, object] = {}
    path = Path("/captures/DWARF_DARK/foo/raw_60s_60_0002_20251020-032310186_20C.fits")
    out = enrich_dark_header(header, path)
    assert out["EXPTIME"] == 60.0
    assert out["GAIN"] == 60
    assert out["DATE-OBS"] == "2025-10-20T03:23:10.186"
    assert out["CCD-TEMP"] == 20.0


def test_enrich_dark_preserves_header_value_when_present() -> None:
    """A reliable header value is preferred over the filename so we don't
    lose precision (e.g. on temperature)."""
    header = {"EXPTIME": 60.0, "GAIN": 60, "CCD-TEMP": 20.5}
    path = Path("/x/raw_60s_60_0002_20251020-032310186_20C.fits")
    out = enrich_dark_header(header, path)
    assert out["CCD-TEMP"] == 20.5  # not 20.0 from the filename


def test_enrich_dark_off_spec_filename_no_op() -> None:
    """If the filename doesn't match the user-dark schema, we drop the
    junk fields but don't invent anything."""
    header = {"OBJECT": "M 31"}
    out = enrich_dark_header(header, Path("/x/some-random-name.fits"))
    assert "OBJECT" not in out
    assert "EXPTIME" not in out
