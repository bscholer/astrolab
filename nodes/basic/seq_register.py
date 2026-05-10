"""seq_register: align a sequence (platesolve-based by default).

Input port  : sequence (SEQUENCE_FITS) - typically bg-extracted calibrated lights
Output port : sequence (SEQUENCE_FITS) - r_<basename>_*.fit or r_<basename>.fit

Two alignment methods are supported:

- `platesolve` (default): runs `seqplatesolve` which writes WCS into each
  frame, then `seqapplyreg` which reprojects them onto a common grid using
  the astrometry. This is what Naztronomy's smart-telescope pipeline does
  and is more robust against star-poor fields, dithered captures, and
  moving targets — Dwarf 3 frames carry RA/DEC headers so it Just Works.

- `star` (legacy): `register -2pass` (star-pattern matching) plus
  `seqapplyreg`. Useful when frames have no usable astrometric headers.

In both modes seqapplyreg writes the actual r_<basename>_*.fit files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nodes.base import Node
from nodes.basic.calibrate import _quote, _stage_sequence
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class SeqRegisterParams(BaseModel):
    input_basename: str = Field(
        default="bkg_pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the input sequence. Output is prefixed with 'r_'. "
        "Default 'bkg_pp_light' assumes the canned pipeline order: convert -> "
        "calibrate -> bg_extract -> register.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=True,
        description="Operate on a FITSEQ container instead of per-frame files.",
        json_schema_extra={"ui_hidden": True},
    )
    method: Literal["platesolve", "star"] = Field(
        default="platesolve",
        description="Alignment strategy: 'platesolve' (default) writes WCS via "
        "seqplatesolve and reprojects frames; 'star' uses star-pattern matching "
        "via register -2pass. Platesolve is what Naztronomy uses and what we "
        "want for OSC smart-telescope captures with proper RA/DEC headers.",
    )
    # --- platesolve method ---
    distortion: bool = Field(
        default=True,
        description="Pass -disto=ps_distortion to seqplatesolve so optical "
        "distortion is modeled when reprojecting. Almost always wanted on "
        "wide-field smart telescopes.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "platesolve"},
        },
    )
    # --- star method ---
    two_pass: bool = Field(
        default=True,
        description="(star method only) Pass -2pass for the refinement step.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "star"},
        },
    )
    transform: Literal["homography", "affine", "similarity", "shift"] = Field(
        default="homography",
        description="(star method only) Transform model.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "star"},
        },
    )
    min_pairs: int = Field(
        default=10,
        ge=4,
        le=200,
        description="(star method only) Minimum star pairs needed.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "star"},
        },
    )
    # --- shared seqapplyreg flags ---
    framing: Literal["max", "min", "cog", "first", "none"] = Field(
        default="max",
        description="seqapplyreg framing: 'max' (default) keeps the union of "
        "all frame footprints, so dithered captures don't get cropped to the "
        "intersection. 'min' is the old default and crops aggressively.",
        json_schema_extra={"ui_section": "advanced"},
    )
    kernel: Literal["square", "nearest", "cubic", "lanczos2", "lanczos3"] = Field(
        default="square",
        description="seqapplyreg interpolation kernel. 'square' (Naztronomy "
        "default) preserves flux; 'lanczos3' is sharper but can introduce "
        "ringing on bright stars.",
        json_schema_extra={"ui_section": "advanced"},
    )
    filter_fwhm: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Drop frames whose FWHM is in the worst N fraction. 0.2 keeps "
        "the best 80%. None disables FWHM filtering.",
    )
    filter_round: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Drop frames whose roundness is in the worst N fraction.",
    )
    # --- drizzle (subpixel reconstruction) ---
    # Drizzle (Fruchter & Hook) reprojects each input pixel onto a finer output
    # grid using a shrunken footprint ('drop'), recovering subpixel detail from
    # dithered captures. Smart telescopes like Dwarf 3 dither between subs, so
    # this is meaningful for them. Off by default: scale=2 quadruples pixel
    # count, RAM, and downstream stack cost.
    drizzle: bool = Field(
        default=False,
        description="Enable drizzle (Fruchter & Hook subpixel reconstruction) "
        "during seqapplyreg. Quadruples pixel count and stack memory at scale=2; "
        "use only when you've verified your captures are sufficiently dithered.",
        json_schema_extra={
            "ui_warning_when_true": (
                "Drizzle ~4x's stack storage and RAM at scale=2. "
                "Re-stacking with drizzle off keeps the original cache lineage."
            ),
        },
    )
    drizzle_scale: Literal[1, 2, 3] = Field(
        default=2,
        description="Drizzle output scale factor. 2 is the standard choice and "
        "what Dwarf 3 captures justify; 1 just bypasses the upscale; 3 is "
        "rarely worthwhile and quickly memory-bound.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"drizzle": True},
        },
    )
    drizzle_dropsize: float = Field(
        default=0.7, ge=0.1, le=1.0,
        description="Drop size as a fraction of input pixel size. 0.7 is the "
        "Siril/HST default; smaller drops give sharper output but need more "
        "frames to fill in coverage gaps.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"drizzle": True},
            "hash_precision": 2,
        },
    )


@register("seq_register")
class SeqRegisterNode(Node[SeqRegisterParams]):
    id = "seq_register"
    version = 1
    cost = "expensive"

    inputs = {"sequence": PortType.SEQUENCE_FITS}
    outputs = {"sequence": PortType.SEQUENCE_FITS}
    params_schema = SeqRegisterParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: SeqRegisterParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path

        if not seq_in.exists():
            raise RuntimeError(f"seq_register: input dir does not exist: {seq_in}")

        seq_out = out_dir_path / "sequence"
        seq_out.mkdir(parents=True, exist_ok=True)

        staged = _stage_sequence(seq_in, seq_out, params.input_basename, params.fitseq)
        if not staged:
            raise RuntimeError(
                f"seq_register: no input frames matching basename "
                f"'{params.input_basename}' under {seq_in}"
            )

        # seqapplyreg writes the r_<basename>_*.fit files in both methods.
        # Filter options drop low-quality frames pre-stack.
        apply_opts: list[str] = [
            f"-framing={params.framing}",
            f"-kernel={params.kernel}",
        ]
        if params.filter_fwhm is not None:
            apply_opts.append(f"-filter-fwhm={params.filter_fwhm}")
        if params.filter_round is not None:
            apply_opts.append(f"-filter-round={params.filter_round}")
        if params.drizzle:
            # Drizzle reuses the same `r_` output prefix; only the dimensions
            # change. Cache key already differs because the params are part
            # of the seq_register hash, so drizzle-on/off get distinct cache
            # lineages without touching upstream calibrate/convert.
            apply_opts.append("-drizzle")
            apply_opts.append(f"-scale={params.drizzle_scale}")
            apply_opts.append(f"-pixfrac={params.drizzle_dropsize}")

        align_commands: list[str]
        if params.method == "platesolve":
            ctx.progress(0.2, f"seq_register: plate-solving {len(staged)} frames")
            ps_opts = ["-nocache", "-force"]
            if params.distortion:
                ps_opts.append("-disto=ps_distortion")
            align_commands = [
                f"seqplatesolve {params.input_basename} {' '.join(ps_opts)}",
                f"seqapplyreg {params.input_basename} {' '.join(apply_opts)}".rstrip(),
            ]
        else:  # star
            ctx.progress(0.2, f"seq_register: star-aligning {len(staged)} frames")
            reg_opts: list[str] = [
                f"-transf={params.transform}",
                f"-minpairs={params.min_pairs}",
            ]
            if params.two_pass:
                reg_opts.append("-2pass")
            align_commands = [
                f"register {params.input_basename} {' '.join(reg_opts)}",
                f"seqapplyreg {params.input_basename} {' '.join(apply_opts)}".rstrip(),
            ]

        commands = [
            f"cd {_quote(seq_out.resolve())}",
            *align_commands,
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=seq_out,
            # Two Siril sub-commands in succession (platesolve+applyreg or
            # register+applyreg). phases=2 partitions the [0.2, 0.95] band so
            # the second command's fresh 0% sweep advances to the upper half
            # instead of visually rewinding the bar to zero.
            on_log=make_progress_handler(ctx, phases=2),
            cancel=ctx.cancel,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"seq_register: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        out_basename = f"r_{params.input_basename}"
        if params.fitseq:
            expected = seq_out / f"{out_basename}.fit"
            if not expected.exists():
                raise RuntimeError(
                    f"seq_register: siril returned 0 but FITSEQ container "
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
                    f"seq_register: siril returned 0 but no {out_basename}_*.fit* "
                    f"frames landed in {seq_out}.\n--- stdout (tail) ---\n"
                    f"{result.stdout[-2000:]}"
                )
            wrote = f"{len(frames)} frames"

        for link in staged:
            if link.is_symlink() or link.exists():
                link.unlink()

        ctx.progress(1.0, f"seq_register: wrote {wrote}")
        return {
            "sequence": Ref(
                node_hash="",
                port="sequence",
                path=seq_out,
                type=PortType.SEQUENCE_FITS,
            )
        }
