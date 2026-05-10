"""convert_lights: first real Siril node.

Takes a directory of raw light FITS files and produces a Siril FITSEQ
sequence ready for downstream calibration / registration / stacking. This
is the first step of the Naztronomy-style pipeline (`convert light
-out=../process`).

Input port  : `lights`   (SEQUENCE_FITS) -> Ref to a directory of .fits files
Output port : `sequence` (SEQUENCE_FITS) -> Ref to a directory containing
                                            <basename>_.seq + frames

The output dir is the natural cache entry. Downstream nodes `cd` into it and
continue with the sequence.

The node symlinks rather than copies inputs so a 1000-frame session does not
double on disk just to satisfy Siril's "current dir" convention. If the input
volume does not support symlinks (e.g. a fat32 SD card), Siril will fail; we
let that surface as a runtime error rather than silently degrading.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from astropy.io import fits
from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class ConvertLightsParams(BaseModel):
    basename: str = Field(
        default="light",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Output sequence basename. Siril writes <basename>_.seq + "
        "<basename>_NNNNN.fit. Keep ASCII-safe; Siril is picky about pathing.",
        json_schema_extra={"ui_hidden": True},
    )

    debayer: bool = Field(
        default=False,
        description="Pass -debayer to convert. Off by default since most pipelines "
        "debayer downstream after calibration; flip on only for OSC flows that "
        "skip calibration.",
        json_schema_extra={"ui_section": "advanced"},
    )

    fitseq: bool = Field(
        default=True,
        description="Emit a single FITSEQ container instead of N individual frames. "
        "Saves filesystem inodes and makes downstream cd's trivial.",
        json_schema_extra={"ui_hidden": True},
    )


@register("convert_lights")
class ConvertLightsNode(Node[ConvertLightsParams]):
    id = "convert_lights"
    version = 1
    cost = "medium"

    inputs = {"lights": PortType.SEQUENCE_FITS}
    outputs = {"sequence": PortType.SEQUENCE_FITS}
    params_schema = ConvertLightsParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: ConvertLightsParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        lights_ref = inputs["lights"]
        src_dir = lights_ref.path

        if not src_dir.exists():
            raise RuntimeError(f"convert_lights: input dir does not exist: {src_dir}")

        # Stage symlinks into a clean dir so Siril's `convert <basename>`
        # picks up exactly the files we want and nothing else (Dwarf folders
        # contain shotsInfo.json, stacked-* artifacts, previews, etc.).
        staging = out_dir_path / "_inputs"
        staging.mkdir(parents=True, exist_ok=True)

        candidates = sorted(_iter_input_fits(src_dir))
        if not candidates:
            raise RuntimeError(f"convert_lights: no .fits files found under {src_dir}")

        # Drop corrupt/truncated subs before they reach Siril. Dwarf 3 occasionally
        # writes partial frames when an exposure is aborted mid-write; Siril's
        # convert accepts them but `calibrate` aborts the entire sequence the
        # moment it hits one, wasting the whole multi-hour run.
        fits_files: list[Path] = []
        skipped: list[tuple[Path, str]] = []
        for f in candidates:
            ok, reason = _is_complete_fits(f)
            if ok:
                fits_files.append(f)
            else:
                skipped.append((f, reason or "unknown"))
        if skipped:
            for f, reason in skipped:
                ctx.log.warning("convert_lights: skipping %s (%s)", f.name, reason)
        if not fits_files:
            raise RuntimeError(
                f"convert_lights: every candidate under {src_dir} was corrupt or "
                f"unreadable ({len(skipped)} skipped)"
            )

        ctx.progress(
            0.05,
            f"convert_lights: staging {len(fits_files)} frames"
            + (f" ({len(skipped)} corrupt skipped)" if skipped else ""),
        )
        for f in fits_files:
            link = staging / f.name
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(f.resolve())

        # Output directory for the converted sequence. Siril writes:
        #   <out>/sequence/<basename>_.seq
        #   <out>/sequence/<basename>_<NNNN>.fit (or one .fits with -fitseq)
        seq_dir = out_dir_path / "sequence"
        seq_dir.mkdir(parents=True, exist_ok=True)

        ctx.progress(0.2, "convert_lights: invoking siril")
        opts: list[str] = [f"-out={seq_dir.resolve()}"]
        if params.fitseq:
            opts.append("-fitseq")
        if params.debayer:
            opts.append("-debayer")

        commands = [
            f"cd {_quote(staging.resolve())}",
            f"convert {params.basename} {' '.join(opts)}",
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=staging,
            on_log=make_progress_handler(ctx),
            cancel=ctx.cancel,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"convert_lights: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        # Sanity-check the expected output exists. Siril 1.4's `convert` writes
        # one entry per input frame: with -fitseq a single `<basename>.fit`
        # FITSEQ container, otherwise one `<basename>_NNNNN.fit` per input
        # (symlink in 1.4) plus a `<basename>_conversion.txt` log. The .seq
        # index file is generated lazily by downstream commands like
        # `seqplatesolve` or `seqload`, so we don't insist on it here.
        if params.fitseq:
            expected = seq_dir / f"{params.basename}.fit"
            if not expected.exists():
                raise RuntimeError(
                    f"convert_lights: siril returned 0 but FITSEQ container "
                    f"{expected} is missing.\n--- stdout (tail) ---\n"
                    f"{result.stdout[-2000:]}"
                )
            wrote = expected.name
        else:
            frames = sorted(
                p
                for p in seq_dir.iterdir()
                if p.name.startswith(f"{params.basename}_") and p.suffix in (".fit", ".fits")
            )
            if not frames:
                raise RuntimeError(
                    f"convert_lights: siril returned 0 but no "
                    f"{params.basename}_*.fit* frames landed in {seq_dir}.\n"
                    f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
                )
            if len(frames) != len(fits_files):
                raise RuntimeError(
                    f"convert_lights: expected {len(fits_files)} converted "
                    f"frames, got {len(frames)}"
                )
            wrote = f"{len(frames)} frames + {params.basename}_conversion.txt"

        ctx.progress(1.0, f"convert_lights: wrote {wrote}")
        return {
            "sequence": Ref(
                node_hash="",
                port="sequence",
                path=seq_dir,
                type=PortType.SEQUENCE_FITS,
            )
        }


def _iter_input_fits(root: Path) -> list[Path]:
    """List .fits files at the top level of `root`, skipping Dwarf artifacts.

    We deliberately do not recurse: a session dir is the expected input shape
    (one flat folder of subs). For mosaic captures the catalog should
    materialize a flat staging dir before invoking convert_lights, since
    Siril's convert wants one basename per dir.
    """
    out: list[Path] = []
    for f in sorted(root.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".fit", ".fits"):
            continue
        name = f.name
        if name.startswith("stacked-") or name.startswith("img_"):
            continue
        out.append(f)
    return out


def _quote(path: Path) -> str:
    """Quote a path for the .ssf script body. Siril's ssf grammar accepts
    double-quoted paths with spaces; backslash-escape any embedded quotes."""
    s = str(path)
    if any(c in s for c in (" ", "\t", '"')):
        return '"' + s.replace('"', r"\"") + '"'
    return s


def _is_complete_fits(path: Path) -> tuple[bool, str | None]:
    """Cheap header-only check that `path` is a structurally complete FITS.

    Reads the primary HDU header and verifies the on-disk size is at least
    `data_offset + padded(NAXIS1 * ... * |BITPIX|/8)`. Catches Dwarf 3
    partial-write artifacts (a header-valid FITS truncated mid-data block)
    without touching pixel data, so 1700-frame sessions stay millisecond-cheap.
    """
    try:
        actual = path.stat().st_size
    except OSError as exc:
        return False, f"stat failed: {exc!r}"
    if actual == 0:
        return False, "zero bytes"
    try:
        with warnings.catch_warnings():
            # astropy emits "may have been truncated" on the very files we want
            # to skip; we report it ourselves, so silence the duplicate noise.
            warnings.simplefilter("ignore", fits.verify.VerifyWarning)
            warnings.filterwarnings("ignore", message=".*truncated.*", module="astropy.io.fits")
            with fits.open(path, memmap=False, do_not_scale_image_data=True) as hdul:
                hdu = hdul[0]
                header = hdu.header
                naxis = int(header.get("NAXIS", 0))
                if naxis < 2:
                    # No image array (e.g. table-only). Leave to downstream code.
                    return True, None
                bitpix = int(header["BITPIX"])
                dims = [int(header[f"NAXIS{i + 1}"]) for i in range(naxis)]
                data_offset = int(hdu.fileinfo()["datLoc"])
    except Exception as exc:
        return False, f"unreadable header: {exc!r}"
    data_bytes = abs(bitpix) // 8
    for d in dims:
        data_bytes *= d
    padded = ((data_bytes + 2879) // 2880) * 2880
    expected = data_offset + padded
    if actual < expected:
        return False, f"truncated: {actual}/{expected} bytes"
    return True, None
