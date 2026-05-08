"""seq_offset: add a constant pedestal to every frame in a sequence.

Input port  : sequence (SEQUENCE_FITS)
Output port : sequence (SEQUENCE_FITS) - same basename, offset applied

OSC smart-telescope data calibrated against a factory dark commonly comes
out predominantly negative — the dark has bias baked in and a faint dark
sky leaves most pixels just below zero after subtraction. Siril 1.4's
seqsubsky refuses to run on negative-pixel data ("removing the gradient
on negative images is not supported"), so we add a small positive
pedestal here.

This is implemented in Python (astropy) because Siril 1.4 has no
seqoffset command — only single-image offset. Per-frame Siril scripts
would work but are slower than direct fits I/O.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from astropy.io import fits
from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class SeqOffsetParams(BaseModel):
    input_basename: str = Field(
        default="pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the input sequence. Output keeps the same "
        "basename (no prefix) — the pedestal is just baked into the pixel "
        "data, downstream nodes see the same file-naming convention.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=True,
        description="Operate on a FITSEQ container instead of per-frame files.",
        json_schema_extra={"ui_hidden": True},
    )
    pedestal: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        description="Constant added to every pixel in normalized [0,1] units. "
        "Default 0.01 covers the typical -0.005 to -0.01 range we see on "
        "OSC factory-dark calibration. Set to 0 to skip the offset (passes "
        "through unchanged) — useful when calibration already produces "
        "non-negative data.",
        json_schema_extra={"hash_precision": 4},
    )


@register("seq_offset")
class SeqOffsetNode(Node[SeqOffsetParams]):
    id = "seq_offset"
    version = 1
    cost = "cheap"

    inputs = {"sequence": PortType.SEQUENCE_FITS}
    outputs = {"sequence": PortType.SEQUENCE_FITS}
    params_schema = SeqOffsetParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: SeqOffsetParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path

        if not seq_in.exists():
            raise RuntimeError(f"seq_offset: input dir does not exist: {seq_in}")

        seq_out = out_dir_path / "sequence"
        seq_out.mkdir(parents=True, exist_ok=True)

        # Materialize copies (not symlinks) of each input frame so we can
        # bake the pedestal into the pixel data without mutating the
        # upstream cache entry. With pedestal=0 we still copy to keep the
        # cache hit semantics simple — the alternative (symlink) would
        # leave downstream nodes pointing at the wrong cache lineage.
        wrote = _apply_offset(
            seq_in, seq_out, params.input_basename, params.fitseq,
            params.pedestal, ctx,
        )

        ctx.progress(1.0, f"seq_offset: wrote {wrote}")
        return {
            "sequence": Ref(
                node_hash="",
                port="sequence",
                path=seq_out,
                type=PortType.SEQUENCE_FITS,
            )
        }


def _apply_offset(
    seq_in: Path,
    seq_out: Path,
    basename: str,
    fitseq: bool,
    pedestal: float,
    ctx: RunContext,
) -> str:
    """Copy each input frame to seq_out and add `pedestal` to its pixels.

    Returns a human-readable summary of what was written.
    """
    if fitseq:
        src = seq_in / f"{basename}.fit"
        if not src.exists():
            raise RuntimeError(
                f"seq_offset: FITSEQ container {src} not found"
            )
        dst = seq_out / src.name
        if pedestal == 0.0:
            shutil.copy(src, dst)
        else:
            _write_offset(src, dst, pedestal)
        return dst.name

    sources = sorted(
        f for f in seq_in.iterdir()
        if (f.is_file() or f.is_symlink())
        and f.name.startswith(f"{basename}_")
        and f.suffix in (".fit", ".fits")
    )
    if not sources:
        raise RuntimeError(
            f"seq_offset: no input frames matching basename '{basename}_*' "
            f"under {seq_in}"
        )

    from server.runtime import JobCancelled  # local to dodge import cycle

    n = len(sources)
    for i, src in enumerate(sources):
        # 200-frame sequences take real seconds; check cancel between frames
        # so a mid-edit supersede doesn't have to wait for the loop to drain.
        if ctx.cancel.is_set():
            raise JobCancelled()
        dst = seq_out / src.name
        if pedestal == 0.0:
            shutil.copy(src.resolve(), dst)
        else:
            _write_offset(src.resolve(), dst, pedestal)
        # Update progress every ~10 frames so we don't spam the event stream
        # with one event per file on a 200-frame sequence.
        if i % 10 == 0 or i == n - 1:
            ctx.progress(
                0.05 + 0.9 * (i + 1) / n,
                f"seq_offset: wrote {i + 1}/{n}",
            )

    # Copy through the .seq index (and its no-underscore alias) so register
    # can still read the sequence metadata. Same pattern as _stage_sequence
    # in calibrate.py.
    for ext_name in (f"{basename}_.seq", f"{basename}.seq"):
        src_seq = seq_in / ext_name
        if src_seq.exists():
            shutil.copy(src_seq, seq_out / ext_name)

    return f"{n} frames"


def _write_offset(src: Path, dst: Path, pedestal: float) -> None:
    """Read src, add pedestal to pixel data, write to dst (preserving header)."""
    with fits.open(src) as hdul:
        # Copy each HDU with its header intact; bake the offset into the data
        # arrays. astropy.io.fits.HDUList.writeto handles the header normalize.
        new_hdus = []
        for i, hdu in enumerate(hdul):
            if hdu.data is None:
                new_hdus.append(hdu.copy())
                continue
            # Add as float32 to avoid silently widening to float64 on disk.
            new_data = hdu.data.astype("float32", copy=True) + pedestal
            new_hdu = hdu.copy()
            new_hdu.data = new_data
            # Note in the header so it's traceable later.
            new_hdu.header["HISTORY"] = (
                f"astrolab.seq_offset: pedestal={pedestal:g} added"
            )
            new_hdus.append(new_hdu)
        fits.HDUList(new_hdus).writeto(dst, overwrite=True)
