"""Dwarf 3 adapter classification tests."""

from __future__ import annotations

from pathlib import Path

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.catalog.adapter import DiscoveredFrame, DiscoveredMaster
from server.catalog.adapter import lookup as adapter_lookup
from server.catalog.adapters.dwarf3 import DwarfThreeAdapter, _path_ts_to_iso


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def test_path_ts_to_iso_format() -> None:
    assert _path_ts_to_iso("2025-10-21-22-18-55-284") == "2025-10-21T22:18:55.284"


def test_adapter_registered() -> None:
    a = adapter_lookup("dwarf3")
    assert isinstance(a, DwarfThreeAdapter)


def test_classifies_lights(tmp_path: Path) -> None:
    folder = tmp_path / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    _touch(folder / "M 33_30s60_Astro_20251021-221929504_24C.fits")
    _touch(folder / "failed_M 33_30s60_Astro_20251021-224358598_22C.fits")
    _touch(folder / "img_reference.png")  # ignored
    # Dwarf-built stack, must be ignored.
    _touch(folder / "stacked-16_M 33_30s60_Astro_20251021-221900245.fits")

    adapter = DwarfThreeAdapter()
    found = list(adapter.discover(tmp_path))

    fits_paths = [str(d.path.name) for d in found]
    assert "M 33_30s60_Astro_20251021-221929504_24C.fits" in fits_paths
    assert "failed_M 33_30s60_Astro_20251021-224358598_22C.fits" in fits_paths
    assert "img_reference.png" not in fits_paths
    assert not any(p.startswith("stacked-") for p in fits_paths)

    by_name = {d.path.name: d for d in found}
    ok = by_name["M 33_30s60_Astro_20251021-221929504_24C.fits"]
    assert ok.image_type == "LIGHT"
    assert ok.quality == "ok"
    assert ok.session_key == (
        "dwarf3:DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    )
    assert ok.session_hints is not None
    assert ok.session_hints["target_from_path"] == "M 33"
    assert ok.session_hints["exptime_from_path"] == 30.0
    assert ok.session_hints["gain_from_path"] == 60
    assert ok.session_hints["is_mosaic"] is False

    failed = by_name["failed_M 33_30s60_Astro_20251021-224358598_22C.fits"]
    assert failed.quality == "failed"


def test_classifies_darks(tmp_path: Path) -> None:
    dark_root = tmp_path / "DWARF_DARK"
    folder = dark_root / "tele_exp_30_gain_60_bin_1_2025-10-21-00-37-45-393"
    _touch(folder / "raw_30s_60_0000_20251021-003814624_22C.fits")
    _touch(folder / "raw_30s_60_0001_20251021-003844601_22C.fits")

    adapter = DwarfThreeAdapter()
    found = list(adapter.discover(tmp_path))

    assert len(found) == 2
    for d in found:
        assert d.image_type == "DARK"
        assert d.quality == "ok"
        assert d.session_key is None
        assert d.session_hints is not None
        assert d.session_hints["exptime_from_path"] == 30.0
        assert d.session_hints["binning_from_path"] == 1


def test_classifies_cali_frame_masters(tmp_path: Path) -> None:
    """Each master kind has its own filename schema per the Dwarf 3 docs."""
    cali = tmp_path / "CALI_FRAME" / "dark" / "cam_0"
    _touch(cali / "dark_exp_30.000000_gain_60_bin_1_22C_stack_10.fits")
    _touch(cali / "ignored_other_file.fits")  # bad name; skipped

    # Factory flat: gain index + bin + ir filter, no exposure or temperature.
    flat_cam0 = tmp_path / "CALI_FRAME" / "flat" / "cam_0"
    _touch(flat_cam0 / "flat_gain_2_bin_1_ir_1.fits")  # ir_1 = Astro

    # Factory bias: just gain index + bin. No exposure, temperature, or filter.
    bias_cam1 = tmp_path / "CALI_FRAME" / "bias" / "cam_1"
    _touch(bias_cam1 / "bias_gain_2_bin_1.fits")

    adapter = DwarfThreeAdapter()
    found = list(adapter.discover(tmp_path))

    assert all(isinstance(d, DiscoveredMaster) for d in found)
    masters = sorted([d for d in found if isinstance(d, DiscoveredMaster)], key=lambda d: d.kind)
    kinds = [m.kind for m in masters]
    assert kinds == ["bias", "dark", "flat"]

    bias = next(m for m in masters if m.kind == "bias")
    assert bias.camera == "WIDE"
    assert bias.binning == 1
    # Bias deliberately carries no photographic gain / filter / temp.
    assert bias.gain is None
    assert bias.filter is None
    assert bias.ccd_temp is None

    dark = next(m for m in masters if m.kind == "dark")
    assert dark.camera == "TELE"
    assert dark.exptime == 30.0
    assert dark.ccd_temp == 22.0
    assert dark.stack_count == 10
    assert dark.source == "factory"
    assert dark.instrument == "DWARFIII"

    flat = next(m for m in masters if m.kind == "flat")
    assert flat.camera == "TELE"
    assert flat.binning == 1
    assert flat.filter == "Astro"  # ir_1 maps to Astro
    # No photographic gain or temperature on factory flats.
    assert flat.gain is None
    assert flat.ccd_temp is None


def test_lights_and_masters_distinguished(tmp_path: Path) -> None:
    """Adapter yields a mix of frames and masters; types differentiate them."""
    light_folder = tmp_path / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    _touch(light_folder / "M 33_30s60_Astro_20251021-221929504_24C.fits")
    cali = tmp_path / "CALI_FRAME" / "dark" / "cam_0"
    _touch(cali / "dark_exp_30.000000_gain_60_bin_1_22C_stack_10.fits")

    adapter = DwarfThreeAdapter()
    found = list(adapter.discover(tmp_path))
    frames = [d for d in found if isinstance(d, DiscoveredFrame)]
    masters = [d for d in found if isinstance(d, DiscoveredMaster)]
    assert len(frames) == 1
    assert len(masters) == 1


def test_skips_unrecognized_top_level_dirs(tmp_path: Path) -> None:
    user_folder = tmp_path / "wizard_nebula"
    _touch(user_folder / "anything.fits")
    adapter = DwarfThreeAdapter()
    assert list(adapter.discover(tmp_path)) == []
