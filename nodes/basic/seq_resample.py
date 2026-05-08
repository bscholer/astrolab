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

from pathlib import Path

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
    scale: float = Field(
        default=1.0,
        ge=0.1,
        le=1.0,
        description="Downscale factor applied to every frame before the heavy "
        "register/stack stages run. 1.0 = native resolution (default). 0.5 "
        "halves each side (~4x faster downstream); 0.25 quarters each side "
        "(~16x faster). Use small values for fast iteration on stretch / "
        "pedestal / rejection params; bump back to 1.0 for the final render. "
        "Each (scale, params) pairing has its own cache lineage, so flipping "
        "between draft and final is cheap once both have been built.",
        json_schema_extra={"hash_precision": 3},
    )
    interp: str = Field(
        default="cubic",
        pattern=r"^(nearest|bilinear|cubic|lanczos2|lanczos3|area|nogrid)$",
        description="Interpolation kernel passed to Siril seqresample. 'cubic' "
        "(default) is a good speed/quality compromise; 'lanczos3' is sharper "
        "but slower; 'area' is best for downscaling without aliasing.",
        json_schema_extra={"ui_section": "advanced"},
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

        staged = _stage_sequence(seq_in, seq_out, params.input_basename, params.fitseq)
        if not staged:
            raise RuntimeError(
                f"seq_resample: no input frames matching basename "
                f"'{params.input_basename}' under {seq_in}"
            )

        ctx.progress(
            0.2,
            f"seq_resample: scale={params.scale:g} on {len(staged)} frames",
        )
        commands = [
            f"cd {_quote(seq_out.resolve())}",
            (
                f"seqresample {params.input_basename} "
                f"-scale={params.scale:g} "
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

        out_basename = f"rs_{params.input_basename}"
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
