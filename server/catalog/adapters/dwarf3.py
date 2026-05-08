"""Dwarf 3 ingest adapter.

Walks a Dwarf 3 capture root and classifies frames by directory structure.
Layout (verified May 2026):

    DWARF_RAW_TELE_<TARGET>_EXP_<sec>_GAIN_<g>_<YYYY-MM-DD-HH-MM-SS-mmm>/
        <TARGET>_<exp>s<gain>_Astro_<YYYYMMDD-HHMMSS>_<temp>C.fits     # ok lights
        failed_*.fits                                                   # rejected lights
        img_*.png, img_*.tif                                            # ignored

    DWARF_DARK/tele_exp_<sec>_gain_<g>_bin_<b>_<...>/raw_*.fits        # raw darks

    CALI_FRAME/{dark,bias,flat}/cam_*/...                              # pre-built masters
        - SKIPPED in Phase 1.a (those belong in the masters table, not frames).

The adapter does not read FITS data; it only sets fields it can determine
from path conventions (image_type, quality, session_key, session_hints).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from server.catalog.adapter import DiscoveredFrame, register

LIGHT_FOLDER_RE = re.compile(
    r"^DWARF_RAW_TELE_(?P<target>.+?)_EXP_(?P<exp>[\d.]+)_GAIN_(?P<gain>\d+)_"
    r"(?P<ts>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{3})$"
)
"""Matches a Dwarf 3 light-capture session folder. Target may contain spaces
and prefixes like 'MOSAIC_'; we keep that prefix in the captured target so
mosaic sessions remain distinguishable until a later slice splits them out."""

DARK_FOLDER_RE = re.compile(
    r"^tele_exp_(?P<exp>[\d.]+)_gain_(?P<gain>\d+)_bin_(?P<bin>\d+)_"
    r"(?P<ts>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{3})(?:_astro)?$"
)
"""Matches a Dwarf 3 raw-dark session folder under DWARF_DARK/."""


class DwarfThreeAdapter:
    scope_id = "dwarf3"

    def discover(self, root: Path) -> Iterator[DiscoveredFrame]:
        if not root.exists():
            return
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if name == "CALI_FRAME":
                # Pre-built masters; deferred to the masters slice.
                continue
            if name == "DWARF_DARK":
                yield from self._walk_darks(child)
                continue
            m = LIGHT_FOLDER_RE.match(name)
            if m:
                yield from self._walk_lights(child, m.groupdict())
                continue
            # Other top-level directories (e.g. hand-curated 'wizard_nebula',
            # 'NGC7380', 'heart_nebula') are user folders not produced by the
            # Dwarf 3 firmware. Skip in Phase 1.a; they need a different rule.

    def _walk_lights(self, folder: Path, hints: dict) -> Iterator[DiscoveredFrame]:
        session_key = f"dwarf3:{folder.name}"
        session_hints = {
            "target_from_path": hints["target"],
            "exptime_from_path": float(hints["exp"]),
            "gain_from_path": int(hints["gain"]),
            "started_at_from_path": _path_ts_to_iso(hints["ts"]),
            "is_mosaic": hints["target"].startswith("MOSAIC_"),
        }
        for f in sorted(folder.iterdir()):
            if not f.is_file() or f.suffix.lower() != ".fits":
                continue
            name = f.name
            # Dwarf 3 deposits its own stacked-N_<...>.fits artifact alongside the
            # raw subs. EXPTIME on these is total integration time, which would
            # poison the session aggregate if treated as a raw light. Skip; if
            # we want to track Dwarf-built masters they belong in the (deferred)
            # masters table, not the frames table.
            if name.startswith("stacked-") or name.startswith("img_"):
                continue
            quality = "failed" if name.startswith("failed_") else "ok"
            yield DiscoveredFrame(
                path=f,
                image_type="LIGHT",
                quality=quality,
                session_key=session_key,
                session_hints=session_hints,
            )

    def _walk_darks(self, dark_root: Path) -> Iterator[DiscoveredFrame]:
        for folder in sorted(dark_root.iterdir()):
            if not folder.is_dir():
                continue
            m = DARK_FOLDER_RE.match(folder.name)
            if not m:
                continue
            hints = {
                "exptime_from_path": float(m.group("exp")),
                "gain_from_path": int(m.group("gain")),
                "binning_from_path": int(m.group("bin")),
                "started_at_from_path": _path_ts_to_iso(m.group("ts")),
            }
            for f in sorted(folder.iterdir()):
                if not f.is_file() or f.suffix.lower() != ".fits":
                    continue
                yield DiscoveredFrame(
                    path=f,
                    image_type="DARK",
                    quality="ok",
                    session_key=None,
                    session_hints=hints,
                )


def _path_ts_to_iso(ts: str) -> str:
    """Convert 'YYYY-MM-DD-HH-MM-SS-mmm' to ISO 8601 'YYYY-MM-DDTHH:MM:SS.mmm'."""
    parts = ts.split("-")
    if len(parts) != 7:
        return ts
    y, mo, d, hh, mm, ss, mmm = parts
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}.{mmm}"


register(DwarfThreeAdapter())
