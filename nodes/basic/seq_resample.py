"""seq_resample: spatially downscale every frame in a sequence.

Input port  : sequence (SEQUENCE_FITS)
Output port : sequence (SEQUENCE_FITS) - rs_<basename>_*.fit

Wraps Siril 1.4's `seqresample` for fast iteration. Pulling `scale` down
(e.g. 0.5) shrinks each frame by that factor before the expensive
register / stack stages run, which gives ~4x speedup at scale=0.5 and
~16x at scale=0.25. Pixel-statistic params (stretch, sigma rejection,
bg-extract degree) translate accurately between draft and full-res; only
per-pixel sharpness suffers.

Default scale is 1.0 (passthrough — Siril effectively just re-emits the
sequence under the new prefix). Users pull the slider down for fast
iteration loops, then crank it back to 1.0 when they're ready for the
final render. Both lineages stay independently cached so flipping back
and forth is cheap once each has been computed.

Lives between calibrate and offset in the canned template: dark/flat
dimensions still need to match raw lights at calibrate time, but
everything past that operates fine on resampled data.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nodes.base import Node
from nodes.basic.calibrate import _quote, _stage_sequence
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class SeqResampleParams(BaseModel):
    input_basename: str = Field(
        default="pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the input sequence. Output is prefixed with 'rs_'.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=True,
        description="Operate on a FITSEQ container instead of per-frame files.",
        json_schema_extra={"ui_hidden": True},
    )
    mode: Literal["full", "draft"] = Field(
        default="full",
        description="'full' resamples at native resolution (passthrough — "
        "downstream sees frames unchanged). 'draft' downscales each frame to "
        "half resolution before the heavy register/stack stages run, giving "
        "~4x speedup. Use 'draft' for fast iteration on stretch / pedestal / "
        "rejection params; flip back to 'full' for the final render. Each "
        "mode has its own cache lineage so toggling is cheap once both are "
        "built.",
    )
    interp: Literal[
        "nearest", "bilinear", "cubic", "lanczos2", "lanczos3", "area", "nogrid"
    ] = Field(
        default="cubic",
        description="Interpolation kernel passed to Siril seqresample. 'cubic' "
        "(default) is a good speed/quality compromise; 'lanczos3' is sharper "
        "but slower; 'area' is best for downscaling without aliasing.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"mode": "draft"},
        },
    )


@register("seq_resample")
class SeqResampleNode(Node[SeqResampleParams]):
    id = "seq_resample"
    version = 1
    cost = "medium"

    inputs = {"sequence": PortType.SEQUENCE_FITS}
    outputs = {"sequence": PortType.SEQUENCE_FITS}
    params_schema = SeqResampleParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: SeqResampleParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path

        if not seq_in.exists():
            raise RuntimeError(f"seq_resample: input dir does not exist: {seq_in}")

        seq_out = out_dir_path / "sequence"
        seq_out.mkdir(parents=True, exist_ok=True)

        out_basename = f"rs_{params.input_basename}"

        if params.mode == "full":
            # Siril's seqresample at scale=1.0 errors out with "Scale is 1.0,
            # nothing to do." rather than no-op'ing, so we handle the
            # passthrough ourselves: hardlink each frame from upstream into
            # our cache dir under the rs_ prefix and hand-write a .seq file
            # so downstream Siril operations recognize the sequence.
            wrote = _passthrough_link(seq_in, seq_out, params.input_basename, out_basename, params.fitseq, ctx)
        else:
            staged = _stage_sequence(seq_in, seq_out, params.input_basename, params.fitseq)
            if not staged:
                raise RuntimeError(
                    f"seq_resample: no input frames matching basename "
                    f"'{params.input_basename}' under {seq_in}"
                )

            scale = 0.5
            ctx.progress(
                0.2,
                f"seq_resample: mode={params.mode} (scale={scale:g}) on "
                f"{len(staged)} frames",
            )
            commands = [
                f"cd {_quote(seq_out.resolve())}",
                (
                    f"seqresample {params.input_basename} "
                    f"-scale={scale:g} "
                    f"-interp={params.interp} "
                    f"-prefix=rs_"
                ),
            ]
            runtime = SirilRuntime()
            result = runtime.run(
                commands,
                working_dir=seq_out,
                on_log=make_progress_handler(ctx),
                cancel=ctx.cancel,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"seq_resample: siril exited {result.returncode}\n"
                    f"--- ssf ---\n{result.ssf}\n"
                    f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                    f"--- stderr ---\n{result.stderr}"
                )

            # Validate Siril's output landed where expected.
            if params.fitseq:
                expected = seq_out / f"{out_basename}.fit"
                if not expected.exists():
                    raise RuntimeError(
                        f"seq_resample: siril returned 0 but FITSEQ container "
                        f"{expected} is missing.\n--- stdout (tail) ---\n"
                        f"{result.stdout[-2000:]}"
                    )
                wrote = expected.name
            else:
                frames = sorted(
                    p
                    for p in seq_out.iterdir()
                    if p.name.startswith(f"{out_basename}_")
                    and p.suffix in (".fit", ".fits")
                )
                if not frames:
                    raise RuntimeError(
                        f"seq_resample: siril returned 0 but no {out_basename}_*.fit* "
                        f"frames landed in {seq_out}.\n--- stdout (tail) ---\n"
                        f"{result.stdout[-2000:]}"
                    )
                wrote = f"{len(frames)} frames"

            # Drop the staging symlinks so the cache entry only holds resampled output.
            for link in staged:
                if link.is_symlink() or link.exists():
                    link.unlink()

        ctx.progress(1.0, f"seq_resample: wrote {wrote}")
        return {
            "sequence": Ref(
                node_hash="",
                port="sequence",
                path=seq_out,
                type=PortType.SEQUENCE_FITS,
            )
        }


def _passthrough_link(
    seq_in: Path,
    seq_out: Path,
    in_basename: str,
    out_basename: str,
    fitseq: bool,
    ctx: RunContext,
) -> str:
    """Full-mode passthrough: hardlink each frame from upstream into seq_out
    under the new prefix and synthesize a .seq file. Hardlinks (rather than
    copies) keep disk usage flat at full-res; they fall back to copy across
    filesystem boundaries.

    Returns a human-readable summary describing what landed in seq_out.
    """
    if fitseq:
        src = seq_in / f"{in_basename}.fit"
        if not src.exists():
            raise RuntimeError(
                f"seq_resample: FITSEQ container {src} not found"
            )
        dst = seq_out / f"{out_basename}.fit"
        _link_or_copy(src, dst)
        ctx.progress(1.0, f"seq_resample: full passthrough -> {dst.name}")
        return dst.name

    sources = sorted(
        f for f in seq_in.iterdir()
        if (f.is_file() or f.is_symlink())
        and f.name.startswith(f"{in_basename}_")
        and f.suffix in (".fit", ".fits")
    )
    if not sources:
        raise RuntimeError(
            f"seq_resample: no input frames matching basename '{in_basename}_*' "
            f"under {seq_in}"
        )

    n = len(sources)
    for i, src in enumerate(sources):
        # Strip the input prefix and prepend the new one. Files are named
        # like "<basename>_NNNNN.fit"; we want "<new_basename>_NNNNN.fit".
        suffix = src.name[len(in_basename):]  # e.g. "_00001.fit"
        dst = seq_out / f"{out_basename}{suffix}"
        _link_or_copy(src.resolve(), dst)
        if i % 25 == 0 or i == n - 1:
            ctx.progress(
                0.05 + 0.85 * (i + 1) / n,
                f"seq_resample: full passthrough {i + 1}/{n}",
            )

    # Hand-write a Siril .seq file. Format mirrors what `convert` produces
    # (no per-frame size info, no registration data — downstream Siril
    # commands rebuild that lazily on demand). Both `<base>_.seq` and
    # `<base>.seq` resolve to the same data; we write the underscore form
    # since that's Siril 1.4's canonical name.
    seq_path = seq_out / f"{out_basename}_.seq"
    lines = [
        "#Siril sequence file. Contains list of images, selection, registration data and statistics",
        "#S 'sequence_name' start_index nb_images nb_selected fixed_len reference_image version variable_size fz_flag drizzle",
        f"S '{out_basename}_' 1 {n} {n} 5 -1 6 0 0 0",
        "L -1",
    ]
    lines.extend(f"I {i + 1} 1" for i in range(n))
    seq_path.write_text("\n".join(lines) + "\n")

    return f"{n} frames (full passthrough)"


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink src to dst; fall back to a copy if hardlinking fails (eg
    cross-filesystem). dst is overwritten if it already exists."""
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy(src, dst)
