"""seq_bg_extract: per-frame background subtraction via Siril's `seqsubsky`.

Each frame gets its own polynomial background model fit and subtracted, which
removes light-pollution gradients before registration. The Naztronomy
smart-telescope pipeline runs this with `seqsubsky <basename> 1 -samples=10`
between calibrate and register; without it, OSC stacks come out washed and
color-cast even with -rgb_equal applied.

Input port  : sequence (SEQUENCE_FITS) - typically the output of calibrate
Output port : sequence (SEQUENCE_FITS) - bkg_<basename>_*.fit (or fitseq)
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


class SeqBgExtractParams(BaseModel):
    input_basename: str = Field(
        default="pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the input sequence. Output is prefixed with 'bkg_'.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=True,
        description="Operate on a FITSEQ container instead of per-frame files.",
        json_schema_extra={"ui_hidden": True},
    )
    degree: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Polynomial degree for the background model. 1 (linear plane) "
        "is what Naztronomy's script uses and is right for most light-pollution "
        "gradients; bump to 2-3 only if you have curved gradients (vignetting "
        "leaking through bad flats, very wide-field optics).",
    )
    samples: int = Field(
        default=10,
        ge=4,
        le=50,
        description="Number of background samples per frame. Naztronomy uses 10. "
        "More samples = smoother model but slower.",
    )


@register("seq_bg_extract")
class SeqBgExtractNode(Node[SeqBgExtractParams]):
    id = "seq_bg_extract"
    version = 1
    cost = "medium"

    inputs = {"sequence": PortType.SEQUENCE_FITS}
    outputs = {"sequence": PortType.SEQUENCE_FITS}
    params_schema = SeqBgExtractParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: SeqBgExtractParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path

        if not seq_in.exists():
            raise RuntimeError(f"seq_bg_extract: input dir does not exist: {seq_in}")

        seq_out = out_dir_path / "sequence"
        seq_out.mkdir(parents=True, exist_ok=True)

        staged = _stage_sequence(seq_in, seq_out, params.input_basename, params.fitseq)
        if not staged:
            raise RuntimeError(
                f"seq_bg_extract: no input frames matching basename "
                f"'{params.input_basename}' under {seq_in}"
            )

        ctx.progress(0.2, f"seq_bg_extract: subtracting background ({len(staged)} frames)")
        commands = [
            f"cd {_quote(seq_out.resolve())}",
            f"seqsubsky {params.input_basename} {params.degree} -samples={params.samples}",
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=seq_out,
            on_log=make_progress_handler(ctx),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"seq_bg_extract: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        out_basename = f"bkg_{params.input_basename}"
        if params.fitseq:
            expected = seq_out / f"{out_basename}.fit"
            if not expected.exists():
                raise RuntimeError(
                    f"seq_bg_extract: siril returned 0 but FITSEQ container "
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
                    f"seq_bg_extract: siril returned 0 but no {out_basename}_*.fit* "
                    f"frames landed in {seq_out}.\n--- stdout (tail) ---\n"
                    f"{result.stdout[-2000:]}"
                )
            wrote = f"{len(frames)} frames"

        for link in staged:
            if link.is_symlink() or link.exists():
                link.unlink()

        ctx.progress(1.0, f"seq_bg_extract: wrote {wrote}")
        return {
            "sequence": Ref(
                node_hash="",
                port="sequence",
                path=seq_out,
                type=PortType.SEQUENCE_FITS,
            )
        }
