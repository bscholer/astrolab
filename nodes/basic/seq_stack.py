"""seq_stack: produce a single stacked image from a registered sequence.

Input port  : sequence (SEQUENCE_FITS) - typically the output of seq_register
Output port : image    (IMAGE_FITS)    - one stacked .fit at out_dir/image.fit

Wraps Siril 1.4's `stack` command. Defaults to a sigma-clipped (winsorized)
rejection stack with addscale normalization, which is the right default for
calibrated lights of varying transparency. The output filename is fixed at
'image.fit' so the runtime can consistently locate it as the 'image' port.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from pydantic import BaseModel, Field

from nodes.base import Node
from nodes.basic.calibrate import _quote, _stage_sequence
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime


class SeqStackParams(BaseModel):
    input_basename: str = Field(
        default="r_pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the registered input sequence.",
    )
    fitseq: bool = Field(default=True)
    method: str = Field(
        default="rej",
        pattern=r"^(rej|mean|median|min|max|sum)$",
        description="Stacking algorithm. 'rej' (default) is sigma-clipped rejection "
        "and is what you almost always want for deep-sky lights.",
    )
    sigma_low: float = Field(
        default=3.0, ge=0.5, le=10.0,
        description="Lower sigma threshold for rejection. Only used when method='rej'.",
    )
    sigma_high: float = Field(
        default=3.0, ge=0.5, le=10.0,
        description="Upper sigma threshold for rejection.",
    )
    rejection_type: str = Field(
        default="w",
        pattern=r"^(w|s|p|l|m|n)$",
        description="Rejection algorithm: w=winsorized, s=sigma, p=percentile, "
        "l=linearfit, m=median, n=none.",
    )
    norm: str = Field(
        default="addscale",
        pattern=r"^(no|add|addscale|mul|mulscale)$",
        description="Normalization. 'addscale' (additive + scale) is the standard for "
        "deep-sky lights with varying transparency.",
    )
    output_norm: bool = Field(
        default=True,
        description="Pass -output_norm to clip the stacked output to [0,1].",
    )
    weight_from_quality: bool = Field(
        default=False,
        description="Pass -weight=wfwhm to weight by FWHM. Cheap quality boost when "
        "frames vary in seeing; harmless to leave off.",
    )
    rgb_equal: bool = Field(
        default=True,
        description="Pass -rgb_equal so Siril rescales each channel mean to match. "
        "Fixes the pink/green color cast that OSC stacks tend to come out with; "
        "Naztronomy's smart-telescope script always sets this.",
    )
    maximize: bool = Field(
        default=True,
        description="Pass -maximize: stack output spans the union of every frame's "
        "footprint instead of just the first frame's. Matters for dithered or "
        "drift-corrected sessions where edges would otherwise be cropped to the "
        "least-common rectangle.",
    )
    filter_included: bool = Field(
        default=True,
        description="Pass -filter-included so frames marked excluded by upstream "
        "quality assessment (eg seqapplyreg's filter-fwhm/round) are dropped from "
        "the stack. Cheap, off only if you want to ignore prior filtering.",
    )


@register("seq_stack")
class SeqStackNode(Node[SeqStackParams]):
    id = "seq_stack"
    version = 1
    cost = "expensive"

    inputs = {"sequence": PortType.SEQUENCE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = SeqStackParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: SeqStackParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path

        if not seq_in.exists():
            raise RuntimeError(f"seq_stack: input dir does not exist: {seq_in}")

        # Stage inputs in a working dir; final output lands at out_dir/image.fit.
        work_dir = out_dir_path / "_stack"
        work_dir.mkdir(parents=True, exist_ok=True)

        staged = _stage_sequence(seq_in, work_dir, params.input_basename, params.fitseq)
        if not staged:
            raise RuntimeError(
                f"seq_stack: no input frames matching basename "
                f"'{params.input_basename}' under {seq_in}"
            )

        out_image = out_dir_path / "image.fit"

        # Build the stack command. Siril 1.4 syntax (positional, no flags for
        # rejection type or bit depth):
        #   stack <seq> rej <type> <sigma_low> <sigma_high> [-norm=...] [-output_norm] [...]
        # Default output is 32-bit FITS already, so we don't pass -32bits.
        cmd_parts = [f"stack {params.input_basename} {params.method}"]
        if params.method == "rej":
            cmd_parts.append(params.rejection_type)
            cmd_parts.append(f"{params.sigma_low} {params.sigma_high}")
            cmd_parts.append(f"-norm={params.norm}")
        elif params.method in ("mean", "median"):
            cmd_parts.append(f"-norm={params.norm}")
        if params.output_norm:
            cmd_parts.append("-output_norm")
        if params.rgb_equal:
            cmd_parts.append("-rgb_equal")
        if params.maximize:
            cmd_parts.append("-maximize")
        if params.filter_included:
            cmd_parts.append("-filter-included")
        if params.weight_from_quality:
            cmd_parts.append("-weight=wfwhm")
        cmd_parts.append(f"-out={_quote(out_image.resolve())}")

        ctx.progress(0.2, f"seq_stack: stacking {len(staged)} frames ({params.method})")
        commands = [
            f"cd {_quote(work_dir.resolve())}",
            " ".join(cmd_parts),
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=work_dir,
            on_log=lambda line: ctx.log.debug("siril: %s", line),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"seq_stack: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        if not out_image.exists():
            raise RuntimeError(
                f"seq_stack: siril returned 0 but {out_image} is missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        # Toss the staging dir; the cache entry only needs image.fit.
        for link in staged:
            if link.is_symlink() or link.exists():
                link.unlink()
        # Siril may have left an FWHM .reg or similar behind; rmdir then is fine to skip.
        with contextlib.suppress(OSError):
            work_dir.rmdir()

        ctx.progress(1.0, f"seq_stack: wrote {out_image.name}")
        return {
            "image": Ref(
                node_hash="",
                port="image",
                path=out_image,
                type=PortType.IMAGE_FITS,
            )
        }
