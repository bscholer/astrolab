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
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nodes._seq_runner import _check_siril_stack_result, image_ref, quote, stage_sequence
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler

_log = logging.getLogger(__name__)

# Rejection types that require all frames in RAM simultaneously.
_MEMORY_HEAVY_REJECTION = frozenset({"w", "s"})


class SeqStackParams(BaseModel):
    input_basename: str = Field(
        default="r_pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the registered input sequence.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=True,
        json_schema_extra={"ui_hidden": True},
    )
    method: Literal["rej", "mean", "median", "min", "max", "sum"] = Field(
        default="rej",
        description="Stacking algorithm. 'rej' (default) is sigma-clipped rejection "
        "and is what you almost always want for deep-sky lights.",
    )
    sigma_low: float = Field(
        default=3.0, gt=0.0, le=10.0,
        description=(
            "Lower rejection threshold. For sigma-based methods ('w', 's') this is a "
            "sigma multiplier (e.g. 3.0). For percentile rejection ('p') it is a "
            "fraction in (0, 1] — e.g. 0.1 means reject the lowest 10%."
        ),
        json_schema_extra={"ui_when": {"method": "rej"}},
    )
    sigma_high: float = Field(
        default=3.0, gt=0.0, le=10.0,
        description=(
            "Upper rejection threshold. For sigma-based methods ('w', 's') this is a "
            "sigma multiplier (e.g. 3.0). For percentile rejection ('p') it is a "
            "fraction in (0, 1] — e.g. 0.1 means reject the highest 10%."
        ),
        json_schema_extra={"ui_when": {"method": "rej"}},
    )
    rejection_type: Literal["w", "s", "p", "l", "m", "n"] = Field(
        default="w",
        description="Rejection algorithm: w=winsorized, s=sigma, p=percentile, "
        "l=linearfit, m=median, n=none.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "rej"},
        },
    )
    norm: Literal["no", "add", "addscale", "mul", "mulscale"] = Field(
        default="addscale",
        description="Normalization. 'addscale' (additive + scale) is the standard for "
        "deep-sky lights with varying transparency.",
        json_schema_extra={"ui_section": "advanced"},
    )
    output_norm: bool = Field(
        default=True,
        description="Pass -output_norm to clip the stacked output to [0,1].",
        json_schema_extra={"ui_section": "advanced"},
    )
    weight_from_quality: bool = Field(
        default=False,
        description="Pass -weight=wfwhm to weight by FWHM. Cheap quality boost when "
        "frames vary in seeing; harmless to leave off.",
        json_schema_extra={"ui_section": "advanced"},
    )
    rgb_equal: bool = Field(
        default=True,
        description="Pass -rgb_equal so Siril rescales each channel mean to match. "
        "Fixes the pink/green color cast that OSC stacks tend to come out with; "
        "Naztronomy's smart-telescope script always sets this.",
        json_schema_extra={"ui_section": "advanced"},
    )
    maximize: bool = Field(
        default=True,
        description="Pass -maximize: stack output spans the union of every frame's "
        "footprint instead of just the first frame's. Matters for dithered or "
        "drift-corrected sessions where edges would otherwise be cropped to the "
        "least-common rectangle.",
        json_schema_extra={"ui_section": "advanced"},
    )
    filter_included: bool = Field(
        default=True,
        description="Pass -filter-included so frames marked excluded by upstream "
        "quality assessment (eg seqapplyreg's filter-fwhm/round) are dropped from "
        "the stack. Cheap, off only if you want to ignore prior filtering.",
        json_schema_extra={"ui_section": "advanced"},
    )


@register("seq_stack")
class SeqStackNode(Node[SeqStackParams]):
    id = "seq_stack"
    version = 2
    cost = "expensive"
    uses_siril = True

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

        staged = stage_sequence(seq_in, work_dir, params.input_basename, params.fitseq)
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
        cmd_parts.append(f"-out={quote(out_image.resolve())}")

        ctx.progress(0.2, f"seq_stack: stacking ({params.method})")
        commands = [
            f"cd {quote(work_dir.resolve())}",
            " ".join(cmd_parts),
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=work_dir,
            on_log=make_progress_handler(ctx),
            cancel=ctx.cancel,
        )
        primary_error: RuntimeError | None = None
        try:
            _check_siril_stack_result(result, node_name="seq_stack", out_image=out_image)
        except RuntimeError as exc:
            primary_error = exc

        if primary_error is not None:
            # Attempt OOM fallback: retry with percentile rejection if the
            # configured rejection is memory-heavy and the output is absent.
            can_retry = (
                params.method == "rej"
                and params.rejection_type in _MEMORY_HEAVY_REJECTION
                and not out_image.exists()
            )
            if not can_retry:
                raise primary_error

            _log.warning(
                "seq_stack: %s rejection ran out of memory at %s frames; "
                "retrying with percentile rejection (0.1/0.1). "
                "Consider increasing Docker memory if you want %s results.",
                {"w": "winsor", "s": "sigma"}[params.rejection_type],
                params.input_basename,
                {"w": "winsor", "s": "sigma"}[params.rejection_type],
            )

            # Build percentile fallback command.
            fallback_parts = [f"stack {params.input_basename} {params.method}"]
            fallback_parts.append("p")
            fallback_parts.append("0.1 0.1")
            fallback_parts.append(f"-norm={params.norm}")
            if params.output_norm:
                fallback_parts.append("-output_norm")
            if params.rgb_equal:
                fallback_parts.append("-rgb_equal")
            if params.maximize:
                fallback_parts.append("-maximize")
            if params.filter_included:
                fallback_parts.append("-filter-included")
            if params.weight_from_quality:
                fallback_parts.append("-weight=wfwhm")
            fallback_parts.append(f"-out={quote(out_image.resolve())}")

            ctx.progress(0.5, "seq_stack: retrying with percentile rejection")
            fallback_commands = [
                f"cd {quote(work_dir.resolve())}",
                " ".join(fallback_parts),
            ]
            fallback_result = runtime.run(
                fallback_commands,
                working_dir=work_dir,
                on_log=make_progress_handler(ctx),
                cancel=ctx.cancel,
            )
            try:
                _check_siril_stack_result(
                    fallback_result, node_name="seq_stack", out_image=out_image
                )
            except RuntimeError:
                # Retry also failed: surface the original error.
                raise primary_error from None

        # Toss the staging dir; the cache entry only needs image.fit.
        from nodes._seq_runner import drop_staged
        drop_staged(staged)
        # Siril may have left an FWHM .reg or similar behind; rmdir is fine to skip.
        with contextlib.suppress(OSError):
            work_dir.rmdir()

        ctx.progress(1.0, f"seq_stack: wrote {out_image.name}")
        return {"image": image_ref(out_image)}
