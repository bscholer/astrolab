"""Tests for the universal scope/image-type classifier.

The classifier is a pure function over ``(header_dict, Path)`` — these
tests never touch the filesystem beyond using ``Path`` as a value type.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.catalog.classify import (
    classify,
    detect_image_type,
    detect_scope,
)
from tests._fits_fixtures import (
    DEFAULT_DARK_HEADER,
    DEFAULT_LIGHT_HEADER,
    asiair_light_header,
    nina_light_header,
    seestar_light_header,
)


# ---------- scope detection ----------------------------------------------


def test_detects_dwarf3_by_telescop() -> None:
    assert detect_scope({"TELESCOP": "DWARFIII"}) == "dwarf3"


def test_detects_dwarf3_by_origin_when_telescop_missing() -> None:
    assert detect_scope({"ORIGIN": "DWARFLAB"}) == "dwarf3"


def test_detects_asiair_by_creator() -> None:
    assert detect_scope({"CREATOR": "ZWO ASIAIR Plus"}) == "asiair"
    assert detect_scope({"CREATOR": "ZWO ASIAIR Pro"}) == "asiair"


def test_detects_nina_by_swcreate() -> None:
    assert detect_scope({"SWCREATE": "N.I.N.A. 3.2.0.3005 (x64)"}) == "nina"


def test_detects_seestar_by_creator() -> None:
    assert detect_scope({"CREATOR": "ZWO Seestar S50"}) == "seestar"
    assert detect_scope({"CREATOR": "ZWO Seestar S30 Pro"}) == "seestar"


def test_detects_seestar_by_instrume_fallback() -> None:
    """If CREATOR is missing, Seestar can still be identified by INSTRUME."""
    assert detect_scope({"INSTRUME": "Seestar S50"}) == "seestar"


def test_returns_none_for_unknown_scope() -> None:
    assert detect_scope({}) is None
    assert detect_scope({"INSTRUME": "Random Camera"}) is None
    assert detect_scope({"CREATOR": "Some Other Tool"}) is None


# ---------- image type detection ----------------------------------------


@pytest.mark.parametrize(
    "imagetyp,expected",
    [
        ("LIGHT", "LIGHT"),
        ("Light", "LIGHT"),
        ("Light Frame", "LIGHT"),
        ("DARK", "DARK"),
        ("Dark", "DARK"),
        ("FLAT", "FLAT"),
        ("Flats", "FLAT"),
        ("BIAS", "BIAS"),
        ("Offset", "BIAS"),
    ],
)
def test_image_type_from_imagetyp_header(imagetyp: str, expected: str) -> None:
    """Every non-Dwarf scope sets IMAGETYP; we accept the common aliases."""
    got = detect_image_type({"IMAGETYP": imagetyp}, Path("/x.fits"), "asiair")
    assert got == expected


def test_dwarf3_light_classified_by_path_when_imagetyp_missing() -> None:
    """Dwarf 3 light frames carry no IMAGETYP — we infer LIGHT from the path."""
    p = Path("/captures/DWARF_RAW_TELE_M33/M33_30s60_Astro.fits")
    assert detect_image_type({}, p, "dwarf3") == "LIGHT"


def test_dwarf3_dark_classified_by_path() -> None:
    p = Path("/captures/DWARF_DARK/tele_exp_60/raw_60s.fits")
    assert detect_image_type({}, p, "dwarf3") == "DARK"


def test_dwarf3_cali_frame_skipped() -> None:
    """CALI_FRAME files are factory masters — the frames pipeline skips them
    so the dedicated masters walker can pick them up instead."""
    p = Path("/captures/CALI_FRAME/dark/cam_0/dark_exp_30.fits")
    assert detect_image_type({}, p, "dwarf3") is None


def test_non_dwarf3_without_imagetyp_returns_none() -> None:
    """If a non-Dwarf scope's FITS is missing IMAGETYP entirely, we'd rather
    skip the file than guess wrong and pollute the lights pile."""
    assert detect_image_type({}, Path("/x.fits"), "asiair") is None


def test_unknown_imagetyp_value_returns_none() -> None:
    """Better to skip than to silently misfile a frame."""
    got = detect_image_type(
        {"IMAGETYP": "WeirdNonStandardValue"}, Path("/x.fits"), "nina"
    )
    assert got is None


# ---------- top-level classify() ----------------------------------------


def test_classify_dwarf3_light() -> None:
    p = Path("/captures/DWARF_RAW_TELE_M33/M33_30s60_Astro.fits")
    result = classify(DEFAULT_LIGHT_HEADER, p)
    assert result is not None
    assert result.scope_id == "dwarf3"
    assert result.image_type == "LIGHT"
    assert result.quality == "ok"


def test_classify_dwarf3_failed_light_quality() -> None:
    """Dwarf 3 marks rejected subs with a `failed_` filename prefix."""
    p = Path("/captures/DWARF_RAW_TELE_M33/failed_M33_30s60_Astro.fits")
    result = classify(DEFAULT_LIGHT_HEADER, p)
    assert result is not None
    assert result.quality == "failed"


def test_classify_dwarf3_dark_from_path() -> None:
    p = Path("/captures/DWARF_DARK/tele_exp_60/raw_60s.fits")
    result = classify(DEFAULT_DARK_HEADER, p)
    assert result is not None
    assert result.scope_id == "dwarf3"
    assert result.image_type == "DARK"


def test_classify_dwarf3_stacked_artifact_skipped() -> None:
    """The Dwarf 3 firmware drops a stacked-* preview next to the subs;
    it carries Dwarf headers but is not a raw sub."""
    p = Path("/captures/DWARF_RAW_TELE_M33/stacked-16_M33_30s60.fits")
    assert classify(DEFAULT_LIGHT_HEADER, p) is None


def test_classify_dwarf3_png_thumbnail_skipped() -> None:
    """Even with Dwarf headers, .png / .tif are not frames."""
    p = Path("/captures/DWARF_RAW_TELE_M33/img_0001.png")
    assert classify(DEFAULT_LIGHT_HEADER, p) is None


def test_classify_dwarf3_cali_frame_skipped() -> None:
    """CALI_FRAME is handled by the factory-masters walker, not classify()."""
    p = Path("/captures/CALI_FRAME/flat/cam_0/flat_gain_2_bin_1_ir_1.fits")
    # Use a sparse header — these factory files barely have any.
    result = classify({"TELESCOP": "DWARFIII", "BAYERPAT": "RGGB"}, p)
    assert result is None


def test_classify_asiair_light() -> None:
    result = classify(asiair_light_header(), Path("/captures/Light/M31_30s.fit"))
    assert result is not None
    assert result.scope_id == "asiair"
    assert result.image_type == "LIGHT"


def test_classify_nina_light() -> None:
    result = classify(nina_light_header(), Path("/captures/M27/2025-09-16/LIGHT/x.fits"))
    assert result is not None
    assert result.scope_id == "nina"
    assert result.image_type == "LIGHT"


def test_classify_seestar_light() -> None:
    result = classify(seestar_light_header(), Path("/captures/M101_sub/Light_M101.fit"))
    assert result is not None
    assert result.scope_id == "seestar"
    assert result.image_type == "LIGHT"


@pytest.mark.parametrize("imagetyp", ["DARK", "FLAT", "BIAS"])
def test_classify_asiair_calibration_types(imagetyp: str) -> None:
    """ASIAIR / NINA / Seestar all set IMAGETYP for darks, flats, biases."""
    header = asiair_light_header(IMAGETYP=imagetyp)
    result = classify(header, Path(f"/captures/{imagetyp}/x.fit"))
    assert result is not None
    assert result.scope_id == "asiair"
    assert result.image_type == imagetyp


def test_classify_unknown_scope_returns_none() -> None:
    """A FITS file from a scope we don't recognize is silently skipped.
    The scanner counts these and surfaces a total to the UI but does not
    error on them — many capture trees have stray FITS exports."""
    header = {"INSTRUME": "Random Mono CCD", "IMAGETYP": "LIGHT"}
    assert classify(header, Path("/x.fits")) is None
