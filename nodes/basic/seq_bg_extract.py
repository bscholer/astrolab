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

from nodes._seq_runner import quote, run_siril_on_sequence, seq_ref
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime


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
        json_schema_extra={
            "agent_hint": (
                "Higher degree removes curved gradients but risks over-fitting"
                " and eating into faint nebulosity."
            ),
        },
    )
    samples: int = Field(
        default=10,
        ge=4,
        le=50,
        description="Number of background samples per frame. Naztronomy uses 10. "
        "More samples = smoother model but slower.",
        json_schema_extra={
            "agent_hint": (
                "More samples produce a smoother gradient model;"
                " fewer risk a patchy background."
            ),
        },
    )


@register("seq_bg_extract")
class SeqBgExtractNode(Node[SeqBgExtractParams]):
    id = "seq_bg_extract"
    version = 1
    uses_siril = True
    preview_hidden = True

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
        seq_out = out_dir_path / "sequence"
        out_basename = f"bkg_{params.input_basename}"

        ctx.progress(0.2, "seq_bg_extract: subtracting background")
        commands = [
            f"cd {quote(seq_out.resolve())}",
            f"seqsubsky {params.input_basename} {params.degree} -samples={params.samples}",
        ]
        wrote = run_siril_on_sequence(
            node_name="seq_bg_extract",
            seq_in=seq_in,
            seq_out=seq_out,
            commands=commands,
            basename=params.input_basename,
            out_basename=out_basename,
            fitseq=params.fitseq,
            ctx=ctx,
            runtime=SirilRuntime(),
        )

        ctx.progress(1.0, f"seq_bg_extract: wrote {wrote}")
        return {"sequence": seq_ref(seq_out)}
