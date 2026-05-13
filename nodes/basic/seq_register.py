"""seq_register: align a sequence (platesolve-based by default).

Input port  : sequence (SEQUENCE_FITS) - typically bg-extracted calibrated lights
Output port : sequence (SEQUENCE_FITS) - r_<basename>_*.fit or r_<basename>.fit

Two alignment methods are supported:

- `platesolve` (default): runs `seqplatesolve` which writes WCS into each
  frame, then `seqapplyreg` which reprojects them onto a common grid using
  the astrometry. This is what Naztronomy's smart-telescope pipeline does
  and is more robust against star-poor fields, dithered captures, and
  moving targets -- Dwarf 3 frames carry RA/DEC headers so it Just Works.

- `star` (legacy): `register -2pass` (star-pattern matching) plus
  `seqapplyreg`. Useful when frames have no usable astrometric headers.

In both modes seqapplyreg writes the actual r_<basename>_*.fit files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nodes._seq_runner import quote, run_siril_on_sequence, seq_ref
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime


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
        json_schema_extra={
            "agent_hint": (
                "Use 'platesolve' for the most accurate alignment on captures"
                " with RA/DEC headers; 'star' is a fallback for fields"
                " without astrometric data."
            ),
        },
    )
    # --- platesolve method ---
    distortion: bool = Field(
        default=False,
        description="Pass -disto=ps_distortion to seqplatesolve to model "
        "optical distortion during reprojection. Disabled by default because "
        "Siril 1.4's seqplatesolve fails the finalize step when consolidating "
        "the distortion polynomial across long sequences (1900+ frames). On "
        "1.4.2 this surfaced as a SIGSEGV via GLib-GIO g_file_info_get_size "
        "NULL assert; on 1.4.3 it became a clean exit 1 with 'Finalizing "
        "sequence processing failed.' Same underlying bug, unfixed in 1.4.3. "
        "The Dwarf 3 optics are well-corrected enough that WCS-only "
        "registration produces indistinguishable results at 60mm aperture. "
        "Enable only on shorter sequences or after upstream patches the "
        "distortion-finalize path.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "platesolve"},
            "agent_hint": (
                "Enabling improves edge sharpness on wide-field optics but"
                " breaks the finalize step on long Siril 1.4 sequences;"
                " leave off unless you have verified a fixed Siril build."
            ),
        },
    )
    # --- star method ---
    two_pass: bool = Field(
        default=True,
        description="(star method only) Pass -2pass for the refinement step.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "star"},
            "agent_hint": (
                "Keeping this on gives tighter alignment at the cost"
                " of extra processing time."
            ),
        },
    )
    transform: Literal["homography", "affine", "similarity", "shift"] = Field(
        default="homography",
        description="(star method only) Transform model.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "star"},
            "agent_hint": (
                "Homography handles most distortions; use 'shift' only for"
                " very short dithered subs with no rotation."
            ),
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
            "agent_hint": (
                "Lower allows registration on sparse star fields but risks"
                " a bad transform from too few reference stars."
            ),
        },
    )
    # --- shared seqapplyreg flags ---
    framing: Literal["max", "min", "cog", "first", "none"] = Field(
        default="max",
        description="seqapplyreg framing: 'max' (default) keeps the union of "
        "all frame footprints, so dithered captures don't get cropped to the "
        "intersection. 'min' is the old default and crops aggressively.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Use 'max' to keep the full field from dithered sessions;"
                " 'min' tightly crops to the common area."
            ),
        },
    )
    kernel: Literal["square", "nearest", "cubic", "lanczos2", "lanczos3"] = Field(
        default="square",
        description="seqapplyreg interpolation kernel. 'square' (Naztronomy "
        "default) preserves flux; 'lanczos3' is sharper but can introduce "
        "ringing on bright stars.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Lanczos3 looks sharper but can create halos around bright stars;"
                " square is the safe photometry-preserving choice."
            ),
        },
    )
    filter_fwhm: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Drop frames whose FWHM is in the worst N fraction. 0.2 keeps "
        "the best 80%. None disables FWHM filtering.",
        json_schema_extra={
            "agent_hint": (
                "Setting to 0.1-0.3 discards the blurriest frames"
                " and noticeably sharpens the final stack."
            ),
        },
    )
    filter_round: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Drop frames whose roundness is in the worst N fraction.",
        json_schema_extra={
            "agent_hint": (
                "Setting to 0.1-0.2 removes out-of-focus or trailed frames,"
                " reducing elongated stars in the stack."
            ),
        },
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
            "agent_hint": (
                "Enable for noticeably finer detail when frames are well-dithered;"
                " has no benefit and wastes RAM when dither is off."
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
            "agent_hint": (
                "Scale 2 is the sweet spot for most smart-telescope sessions;"
                " scale 3 gives diminishing returns and can exhaust memory."
            ),
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
            "agent_hint": (
                "Smaller drop size sharpens the output but requires many more"
                " frames to fill the grid without gaps."
            ),
        },
    )


@register("seq_register")
class SeqRegisterNode(Node[SeqRegisterParams]):
    id = "seq_register"
    version = 1
    cost = "expensive"
    uses_siril = True

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
        seq_out = out_dir_path / "sequence"

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

        if params.method == "platesolve":
            ctx.progress(0.2, "seq_register: plate-solving sequence")
            ps_opts = ["-nocache", "-force"]
            if params.distortion:
                ps_opts.append("-disto=ps_distortion")
            align_commands: list[str] = [
                f"seqplatesolve {params.input_basename} {' '.join(ps_opts)}",
                f"seqapplyreg {params.input_basename} {' '.join(apply_opts)}".rstrip(),
            ]
        else:  # star
            ctx.progress(0.2, "seq_register: star-aligning sequence")
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

        out_basename = f"r_{params.input_basename}"
        commands = [
            f"cd {quote(seq_out.resolve())}",
            *align_commands,
        ]
        # Two Siril sub-commands in succession (platesolve+applyreg or
        # register+applyreg). phases=2 partitions the [0.2, 0.95] band so
        # the second command's fresh 0% sweep advances to the upper half
        # instead of visually rewinding the bar to zero.
        wrote = run_siril_on_sequence(
            node_name="seq_register",
            seq_in=seq_in,
            seq_out=seq_out,
            commands=commands,
            basename=params.input_basename,
            out_basename=out_basename,
            fitseq=params.fitseq,
            ctx=ctx,
            runtime=SirilRuntime(),
            phases=2,
        )

        ctx.progress(1.0, f"seq_register: wrote {wrote}")
        return {"sequence": seq_ref(seq_out)}
