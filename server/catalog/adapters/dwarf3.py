"""Dwarf 3-specific helpers.

Most of the Dwarf 3 ingest path now runs through the universal classifier
in :mod:`server.catalog.classify`. Two cases still need scope-specific
knowledge:

1. **Factory calibration masters** under ``CALI_FRAME/`` carry almost no
   FITS-header metadata. Exposure, photographic-gain index, IR-band index,
   ccd temperature, and stack depth are all encoded in the filename per
   the Dwarf 3 docs. :func:`walk_factory_masters` reads those filenames
   and yields :class:`DiscoveredMaster` rows the scanner can ingest.

2. **User-captured dark frames** under ``DWARF_DARK/`` ship with a
   primary header but the Dwarf firmware fills it with stale or
   plate-solve-poisoned values (RA / DEC / OBJECT carried over from the
   last light capture; EXPTIME and GAIN sometimes wrong). The filename
   on these is the authoritative source. :func:`enrich_dark_header`
   parses it and overrides the affected header keys before the row is
   written.

These are the only places where path-or-filename parsing is still
load-bearing. Lights are entirely header-driven through the universal
classifier; mosaic panels are picked up because the OBJECT panel suffix
``(N)`` is stripped by :func:`server.catalog.fits_reader.normalize_target`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from server.catalog.models import DiscoveredMaster


DARK_MASTER_RE = re.compile(
    r"^dark_exp_(?P<exp>[\d.]+)_gain_(?P<gain>\d+)_bin_(?P<bin>\d+)_"
    r"(?P<temp>-?\d+(?:\.\d+)?)C_stack_(?P<n>\d+)\.(?:fits|png)$"
)
"""Dark master: ``dark_exp_15.000000_gain_60_bin_1_38C_stack_10.fits``.
Carries exposure, photographic gain, bin mode, ccd temp, and stack depth."""

FLAT_MASTER_RE = re.compile(
    r"^flat_gain_(?P<gain>\d+)_bin_(?P<bin>\d+)_ir_(?P<ir>\d+)\.(?:fits|png)$"
)
"""Flat master: ``flat_gain_2_bin_1_ir_0.fits``.

Per Dwarf docs the ``gain_N`` on factory bias/flat is a different (low)
gain index than the photographic ``GAIN_60`` on lights — we deliberately
don't store it on the master, since matching it against a session's
photographic gain would cause every flat to miss. ``ir_N`` IS the filter
type."""

BIAS_MASTER_RE = re.compile(
    r"^bias_gain_(?P<gain>\d+)_bin_(?P<bin>\d+)\.(?:fits|png)$"
)
"""Bias master: ``bias_gain_2_bin_1.fits``. Only ``bin`` is matchable
(factory bias is filter-, exposure-, and temperature-independent)."""

# Filename of a Dwarf 3 user dark, e.g.
#   raw_60s_60_0002_20251020-032310186_20C.fits
# Captures exposure, gain, frame index, timestamp, ccd temp.
USER_DARK_FILENAME_RE = re.compile(
    r"^raw_(?P<exp>[\d.]+)s_(?P<gain>\d+)_(?P<idx>\d+)_"
    r"(?P<date>\d{8})-(?P<hms>\d{6})(?P<ms>\d{3})_"
    r"(?P<temp>-?\d+(?:\.\d+)?)C\.fits$"
)


FLAT_IR_TO_FILTER: dict[int, str] = {0: "None", 1: "None", 2: "HaOIII"}
"""``ir_N`` index on factory flats -> canonical filter name. Both ``VIS``
(0) and ``Astro`` (1) collapse to ``None`` (no narrowband filter);
``Duo-Band`` (2) is ``HaOIII``. See ``filter_aliases.py``."""

CAM_FROM_DIR: dict[str, str] = {"cam_0": "TELE", "cam_1": "WIDE"}
"""Per Dwarf docs: ``cam_0`` is the telephoto, ``cam_1`` is the wide."""


def walk_factory_masters(root: Path) -> Iterator[DiscoveredMaster]:
    """Yield Dwarf 3 factory calibration masters under ``root/CALI_FRAME``.

    Three subtrees, one per master kind:

        CALI_FRAME/dark/cam_{0,1}/dark_exp_*_gain_*_bin_*_*C_stack_*.fits
        CALI_FRAME/flat/cam_{0,1}/flat_gain_*_bin_*_ir_*.fits
        CALI_FRAME/bias/cam_{0,1}/bias_gain_*_bin_*.fits

    Files whose filenames don't match the per-kind regex are skipped
    silently — Dwarf firmware sometimes drops extra ``.png`` previews and
    similar that aren't ingest targets.
    """
    cali_root = root / "CALI_FRAME"
    if not cali_root.exists():
        return
    for kind_dir in sorted(cali_root.iterdir()):
        if not kind_dir.is_dir():
            continue
        kind = kind_dir.name
        if kind not in {"dark", "flat", "bias"}:
            continue
        for cam_dir in sorted(kind_dir.iterdir()):
            if not cam_dir.is_dir() or not cam_dir.name.startswith("cam_"):
                continue
            camera = CAM_FROM_DIR.get(cam_dir.name)
            for f in sorted(cam_dir.iterdir()):
                if not f.is_file() or f.suffix.lower() not in (".fits", ".png"):
                    continue
                master = _parse_master(f, kind, camera)
                if master is not None:
                    yield master


def _parse_master(
    path: Path, kind: str, camera: str | None
) -> DiscoveredMaster | None:
    if kind == "dark":
        m = DARK_MASTER_RE.match(path.name)
        if not m:
            return None
        return DiscoveredMaster(
            path=path,
            kind="dark",
            source="factory",
            camera=camera,
            instrument="DWARFIII",
            exptime=float(m.group("exp")),
            gain=int(m.group("gain")),
            binning=int(m.group("bin")),
            ccd_temp=float(m.group("temp")),
            stack_count=int(m.group("n")),
        )
    if kind == "flat":
        m = FLAT_MASTER_RE.match(path.name)
        if not m:
            return None
        ir = int(m.group("ir"))
        return DiscoveredMaster(
            path=path,
            kind="flat",
            source="factory",
            camera=camera,
            instrument="DWARFIII",
            # gain/exptime/temp deliberately left None: factory flats
            # encode an `ir_N` filter type and a low-gain mode that don't
            # correspond to a session's photographic settings.
            filter=FLAT_IR_TO_FILTER.get(ir),
            binning=int(m.group("bin")),
        )
    if kind == "bias":
        m = BIAS_MASTER_RE.match(path.name)
        if not m:
            return None
        return DiscoveredMaster(
            path=path,
            kind="bias",
            source="factory",
            camera=camera,
            instrument="DWARFIII",
            # No filter / exptime / temp / photographic gain on factory
            # bias; matching is binning-only.
            binning=int(m.group("bin")),
        )
    return None


def enrich_dark_header(header: dict[str, Any], path: Path) -> dict[str, Any]:
    """Fill in dark-frame header keys from the filename when missing.

    Dwarf 3 user darks (``DWARF_DARK/.../raw_*.fits``) ship with headers
    where ``OBJECT``, ``RA``, ``DEC``, and sometimes ``EXPTIME`` carry
    junk from the last light capture, while ``EXPTIME`` / ``GAIN`` /
    ``DATE-OBS`` / temperature are reliably encoded in the filename.

    Strategy: drop OBJECT / RA / DEC entirely (the universal pipeline
    treats darks as session-less and these fields would otherwise create
    spurious target rows), and fill in EXPTIME / GAIN / DATE-OBS / CCD-TEMP
    from the filename if missing. The returned dict is a copy; the input
    is not mutated.
    """
    out = dict(header)
    # Junk fields the firmware copies forward from the last light.
    for key in ("OBJECT", "RA", "DEC", "FILTER"):
        out.pop(key, None)

    m = USER_DARK_FILENAME_RE.match(path.name)
    if not m:
        return out

    # Fill only when missing. If the header set a value we trust it (some
    # firmwares write reliable values, and overriding correct data with
    # filename-derived data would lose precision e.g. on temperature).
    if "EXPTIME" not in out:
        out["EXPTIME"] = float(m.group("exp"))
    if "GAIN" not in out:
        out["GAIN"] = int(m.group("gain"))
    if "DATE-OBS" not in out:
        date = m.group("date")
        hms = m.group("hms")
        ms = m.group("ms")
        out["DATE-OBS"] = (
            f"{date[0:4]}-{date[4:6]}-{date[6:8]}T"
            f"{hms[0:2]}:{hms[2:4]}:{hms[4:6]}.{ms}"
        )
    if "CCD-TEMP" not in out and "DET-TEMP" not in out:
        out["CCD-TEMP"] = float(m.group("temp"))

    return out
