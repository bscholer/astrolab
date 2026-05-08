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

from server.catalog.adapter import DiscoveredFrame, DiscoveredMaster, register

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

DARK_MASTER_RE = re.compile(
    r"^dark_exp_(?P<exp>[\d.]+)_gain_(?P<gain>\d+)_bin_(?P<bin>\d+)_"
    r"(?P<temp>-?\d+(?:\.\d+)?)C_stack_(?P<n>\d+)\.(?:fits|png)$"
)
"""Dark master: dark_exp_15.000000_gain_60_bin_1_38C_stack_10.fits.

Carries the full set of attributes (exp, photographic gain, bin mode, ccd
temp, stack depth)."""

FLAT_MASTER_RE = re.compile(
    r"^flat_gain_(?P<gain>\d+)_bin_(?P<bin>\d+)_ir_(?P<ir>\d+)\.(?:fits|png)$"
)
"""Flat master: flat_gain_2_bin_1_ir_0.fits.

Per Dwarf docs the `gain_N` on factory bias/flat is a different (low) gain
index than the photographic `GAIN_60` on lights — we deliberately don't
store it on the master, since matching it against a session's photographic
gain would cause every flat to miss. `ir_N` IS the filter type:
0 = VIS, 1 = Astro, 2 = Duo-Band."""

BIAS_MASTER_RE = re.compile(
    r"^bias_gain_(?P<gain>\d+)_bin_(?P<bin>\d+)\.(?:fits|png)$"
)
"""Bias master: bias_gain_2_bin_1.fits. Only `bin` is matchable (factory
bias is filter-, exposure-, and temperature-independent)."""

# Map the `ir_N` index on factory flats to the filter-name string the catalog
# already stores for lights, so the matcher can join them transparently.
FLAT_IR_TO_FILTER: dict[int, str] = {0: "VIS", 1: "Astro", 2: "Duo-Band"}

CAM_FROM_DIR: dict[str, str] = {"cam_0": "TELE", "cam_1": "WIDE"}
"""Per Dwarf docs: cam_0 is the telephoto, cam_1 is the wide."""


class DwarfThreeAdapter:
    scope_id = "dwarf3"

    def discover(self, root: Path) -> Iterator[DiscoveredFrame | DiscoveredMaster]:
        if not root.exists():
            return
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if name == "CALI_FRAME":
                yield from self._walk_cali(child)
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
            # Dwarf 3 firmware. Skip; they would need a different rule.

    def _walk_lights(self, folder: Path, hints: dict) -> Iterator[DiscoveredFrame]:
        session_key = f"dwarf3:{folder.name}"
        raw_target = hints["target"]
        is_mosaic = raw_target.startswith("MOSAIC_")
        # Strip the MOSAIC_ prefix so all panels share one target row.
        target = raw_target[len("MOSAIC_"):] if is_mosaic else raw_target
        session_hints = {
            "target_from_path": target,
            "exptime_from_path": float(hints["exp"]),
            "gain_from_path": int(hints["gain"]),
            "started_at_from_path": _path_ts_to_iso(hints["ts"]),
            "is_mosaic": is_mosaic,
        }

        # Mosaic captures nest one panel folder per panel under the outer
        # session folder, each itself matching DWARF_RAW_TELE_*. Treat the
        # outer folder as the session and yield frames from every panel under
        # the same session_key.
        if is_mosaic:
            for child in sorted(folder.iterdir()):
                if child.is_dir() and LIGHT_FOLDER_RE.match(child.name):
                    yield from self._walk_panel(child, session_key, session_hints)
            return

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

    def _walk_panel(
        self, panel_dir: Path, session_key: str, session_hints: dict
    ) -> Iterator[DiscoveredFrame]:
        for f in sorted(panel_dir.iterdir()):
            if not f.is_file() or f.suffix.lower() != ".fits":
                continue
            name = f.name
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

    def _walk_cali(self, cali_root: Path) -> Iterator[DiscoveredMaster]:
        """Walk CALI_FRAME/{dark,bias,flat}/cam_*/ and yield masters.

        Each kind has its own filename schema; using one regex misses bias
        (no temp/exp) and flat (no temp/exp; carries `ir_N` filter) entirely.
        """
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
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in (".fits", ".png"):
                        continue
                    master = self._parse_master(f, kind, camera)
                    if master is not None:
                        yield master

    def _parse_master(
        self, f: Path, kind: str, camera: str | None
    ) -> DiscoveredMaster | None:
        """Match a master file against the per-kind regex and return its
        DiscoveredMaster, or None if the filename doesn't fit the spec."""
        if kind == "dark":
            m = DARK_MASTER_RE.match(f.name)
            if not m:
                return None
            return DiscoveredMaster(
                path=f,
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
            m = FLAT_MASTER_RE.match(f.name)
            if not m:
                return None
            ir = int(m.group("ir"))
            return DiscoveredMaster(
                path=f,
                kind="flat",
                source="factory",
                camera=camera,
                instrument="DWARFIII",
                # gain/exptime/temp deliberately left None: factory flats
                # encode an `ir_N` filter type and a low-gain mode that
                # don't correspond to a session's photographic settings.
                filter=FLAT_IR_TO_FILTER.get(ir),
                binning=int(m.group("bin")),
            )
        if kind == "bias":
            m = BIAS_MASTER_RE.match(f.name)
            if not m:
                return None
            return DiscoveredMaster(
                path=f,
                kind="bias",
                source="factory",
                camera=camera,
                instrument="DWARFIII",
                # No filter / exptime / temp / photographic gain on factory
                # bias; matching is binning-only.
                binning=int(m.group("bin")),
            )
        return None

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
