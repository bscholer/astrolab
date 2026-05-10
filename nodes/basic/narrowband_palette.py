"""narrowband_palette: split a CFA sequence into Ha + OIII and compose a palette.

Input port  : sequence (SEQUENCE_FITS) - calibrated, NOT debayered. The Bayer
                                         pattern must still be intact so
                                         seqextract_HaOIII can pick out the red
                                         (Ha) and blue/green (OIII) sub-pixels.
Output port : image    (IMAGE_FITS)    - the composed RGB FITS at out_dir/image.fit

This is the OSC dual-band-filter flow that L-eXtreme / L-Pro / Optolong L-Ultimate
users want when they shoot emission nebulae from a smart telescope. The chain of
Siril commands mirrors Naztronomy's Smart-Telescope-PP script verbatim because
that script is the working ground truth and any clever shortcut here breaks the
proven OIII normalization pass.

The flow is:

    seqextract_HaOIII <pp_seq> -resample=ha
        -> Ha_<pp_seq> and OIII_<pp_seq> sub-sequences. -resample=ha upscales
           the Ha channel so it matches the OIII channel's spatial resolution
           after Bayer demosaic; without it, Ha comes out at half-rez.
    register Ha_<pp_seq>; stack r_Ha_<pp_seq> rej 3 3 -norm=addscale ...
        -> results_ha.fit
    register OIII_<pp_seq>; stack r_OIII_<pp_seq> rej 3 3 -norm=addscale ...
        -> results_oiii.fit
    register results -transf=shift -interp=none
        -> shift-only align of the two stacks (no rescaling, no rotation; the
           extracted channels share the same WCS-less local frame, so a
           translation is all that's needed). This produces r_results_ha and
           r_results_oiii.
    pm <OIII normalized to Ha statistics>     (PixelMath)
        -> normalized_r_results_oiii.fit
    [HSO only] pm (Ha * 0.7) + (OIII * 0.3)   (synthetic green)
        -> synthetic_green.fit
    rgbcomp <R> <G> <B> -out=image            per PALETTE_CONFIG.

PALETTE_CONFIG and the OIII-normalization PixelMath formula are copied verbatim
from the Naztronomy script. Palette ideas come from
https://www.cloudynights.com/topic/800240-differences-of-bi-color-pallets-ha-oiii/.

Implementation notes:
- Mode is the only param. Adding more knobs (sigma, rejection type) would let
  users diverge from the Naztronomy ground truth, which is exactly what we
  promised the user we wouldn't do here. Knobs land later if they're asked
  for, with their own version bump.
- The node always operates in fitseq=False mode internally because
  seqextract_HaOIII writes per-frame outputs, not a FITSEQ container. This is
  decoupled from how the upstream sequence was carried.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nodes.base import Node
from nodes.basic.calibrate import _quote, _stage_sequence
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler

# Palette configuration: which extracted channel feeds each RGB output.
# Copied verbatim from Naztronomy's Smart-Telescope-PP.py (lines ~140-170);
# palette ideas from
# https://www.cloudynights.com/topic/800240-differences-of-bi-color-pallets-ha-oiii/.
# Lower-cased keys here so the Pydantic Literal matches Python convention; the
# UI can display them upper-cased on the way out.
PALETTE_CONFIG: dict[str, dict[str, object]] = {
    "hoo": {
        "description": "Ha->Red, OIII->Green+Blue (traditional)",
        "channels": {"R": "ha", "G": "oiii", "B": "oiii"},
    },
    "ohh": {
        "description": "OIII->Red, Ha->Green+Blue (inverted)",
        "channels": {"R": "oiii", "G": "ha", "B": "ha"},
    },
    "hho": {
        "description": "Ha->Red+Green, OIII->Blue (SHO-like)",
        "channels": {"R": "ha", "G": "ha", "B": "oiii"},
    },
    "ooh": {
        "description": "OIII->Red+Green, Ha->Blue (electric blue)",
        "channels": {"R": "oiii", "G": "oiii", "B": "ha"},
    },
    "hoh": {
        "description": "Ha->Red+Blue, OIII->Green (purple nebulae)",
        "channels": {"R": "ha", "G": "oiii", "B": "ha"},
    },
    "oho": {
        "description": "OIII->Red+Blue, Ha->Green (greenish)",
        "channels": {"R": "oiii", "G": "ha", "B": "oiii"},
    },
    "hso": {
        "description": "Ha->Red, Synthetic Green, OIII->Blue (Hubble-style)",
        "channels": {"R": "ha", "G": "synthetic", "B": "oiii"},
        "requires_synthetic": True,
    },
}


PaletteMode = Literal["hoo", "ohh", "hho", "ooh", "hoh", "oho", "hso"]


class NarrowbandPaletteParams(BaseModel):
    input_basename: str = Field(
        default="pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the calibrated CFA sequence. Default 'pp_light' "
        "matches the canned narrowband template (convert -> calibrate without "
        "debayer). The sequence MUST be CFA (Bayer pattern intact); "
        "seqextract_HaOIII reads sub-pixel positions from the Bayer mosaic.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=False,
        description="Operate on a FITSEQ container instead of per-frame files. "
        "Default off because seqextract_HaOIII writes per-frame outputs and "
        "matching the upstream sequence layout makes input staging simpler.",
        json_schema_extra={"ui_hidden": True},
    )
    mode: PaletteMode = Field(
        default="hoo",
        description="Bi-color or tri-color palette to render. 'hoo' is the "
        "traditional Ha-as-red, OIII-as-cyan composition that most OSC "
        "narrowband shooters expect. 'hso' adds a synthetic-green channel for "
        "a Hubble-like tone.",
    )


@register("narrowband_palette")
class NarrowbandPaletteNode(Node[NarrowbandPaletteParams]):
    id = "narrowband_palette"
    version = 1
    cost = "expensive"

    inputs = {"sequence": PortType.SEQUENCE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = NarrowbandPaletteParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: NarrowbandPaletteParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path

        if not seq_in.exists():
            raise RuntimeError(
                f"narrowband_palette: input sequence dir does not exist: {seq_in}"
            )

        work_dir = out_dir_path / "_narrowband"
        work_dir.mkdir(parents=True, exist_ok=True)

        staged = _stage_sequence(
            seq_in, work_dir, params.input_basename, params.fitseq
        )
        if not staged:
            raise RuntimeError(
                f"narrowband_palette: no input frames matching basename "
                f"'{params.input_basename}' under {seq_in}. The sequence "
                f"must be CFA (un-debayered) so seqextract_HaOIII can read "
                f"the Bayer pattern."
            )

        out_image = out_dir_path / "image.fit"

        commands = _build_commands(
            params.input_basename, params.mode, work_dir, out_image
        )

        ctx.progress(
            0.1,
            f"narrowband_palette: extracting Ha/OIII and composing {params.mode.upper()}",
        )
        runtime = SirilRuntime()
        # The chain runs five Siril sub-commands (extract, two register+stack
        # pairs, the shift-only register, the PixelMath block, and rgbcomp).
        # phases=5 keeps the progress bar moving forward instead of resetting
        # for each new sub-command.
        result = runtime.run(
            commands,
            working_dir=work_dir,
            on_log=make_progress_handler(ctx, phases=5),
            cancel=ctx.cancel,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"narrowband_palette: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        if not out_image.exists():
            raise RuntimeError(
                f"narrowband_palette: siril returned 0 but {out_image} is "
                f"missing.\n--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        # Drop input symlinks so the cache entry only carries this node's
        # outputs, then try to drop the staging dir if it's empty.
        for link in staged:
            if link.is_symlink() or link.exists():
                link.unlink()
        with contextlib.suppress(OSError):
            work_dir.rmdir()

        ctx.progress(1.0, f"narrowband_palette: wrote {out_image.name}")
        return {
            "image": Ref(
                node_hash="",
                port="image",
                path=out_image,
                type=PortType.IMAGE_FITS,
            )
        }


def _build_commands(
    input_basename: str, mode: str, work_dir: Path, out_image: Path
) -> list[str]:
    """Render the Siril SSF body for one narrowband composition.

    Lifted directly from Naztronomy's process_single_channel + align_results +
    normalize_oiii_to_ha + create_synthetic_green_channel +
    create_final_hoo_composition pipeline. Kept as a free function so tests can
    introspect the exact command list without spinning up SirilRuntime.
    """
    config = PALETTE_CONFIG[mode]
    channels_map = config["channels"]  # type: ignore[index]
    requires_synthetic = bool(config.get("requires_synthetic", False))

    ha_seq = f"Ha_{input_basename}"
    oiii_seq = f"OIII_{input_basename}"
    ha_stack = "results_ha"
    oiii_stack = "results_oiii"
    # `register results -transf=shift -interp=none` matches results_ha and
    # results_oiii together; Siril sorts the matched files alphabetically and
    # writes r_results_ha and r_results_oiii, so the registered identifiers we
    # plug into PixelMath are the originals prefixed with r_.
    ha_aligned = f"r_{ha_stack}"
    oiii_aligned = f"r_{oiii_stack}"
    normalized_oiii = f"normalized_{oiii_aligned}"
    synthetic_green = "synthetic_green"

    # OIII normalization formula. Match Naztronomy's text exactly so the
    # numeric output is bit-identical to the script's run.
    normalize_formula = (
        f"${oiii_aligned}$*mad(${ha_aligned}$)/mad(${oiii_aligned}$)"
        f"-mad(${ha_aligned}$)/mad(${oiii_aligned}$)*median(${oiii_aligned}$)"
        f"+median(${ha_aligned}$)"
    )
    synthetic_formula = f"(${ha_aligned}$ * 0.7) + (${normalized_oiii}$ * 0.3)"

    cmds: list[str] = [f"cd {_quote(work_dir.resolve())}"]

    # Step 1: split the CFA sequence into Ha and OIII sub-sequences. -resample=ha
    # upscales Ha to match OIII spatial resolution after Bayer demosaic.
    cmds.append(f"seqextract_HaOIII {input_basename} -resample=ha")

    # Steps 2 + 3: register and stack each channel independently.
    for src_seq, out_name in ((ha_seq, ha_stack), (oiii_seq, oiii_stack)):
        cmds.append(f"register {src_seq}")
        cmds.append(
            f"stack r_{src_seq} rej 3 3 -norm=addscale -output_norm -32b "
            f"-out={out_name}"
        )

    # Step 4: shift-only align the two stacks together. Siril picks up
    # `results_ha` and `results_oiii` as a 2-frame sequence named `results`.
    cmds.append("register results -transf=shift -interp=none")

    # Step 5: load the registered OIII stack and run PixelMath to normalize it
    # to Ha's intensity statistics. Saving the result to a deterministic name
    # so the rgbcomp command at the end can reference it by file.
    cmds.append(f"load {oiii_aligned}")
    cmds.append(f"pm {normalize_formula}")
    cmds.append(f"save {normalized_oiii}")

    # Step 6 (HSO only): synthesize a green channel as 0.7*Ha + 0.3*OIII so the
    # rgbcomp can place it in G.
    if requires_synthetic:
        cmds.append(f"pm {synthetic_formula}")
        cmds.append(f"save {synthetic_green}")

    # Step 7: rgbcomp the three channels per the palette table. Synthetic green
    # uses the just-saved synthetic file; everything else maps to either the
    # aligned Ha stack or the normalized OIII stack.
    sources = {
        "ha": ha_aligned,
        "oiii": normalized_oiii,
        "synthetic": synthetic_green,
    }
    r_file = sources[channels_map["R"]]  # type: ignore[index]
    g_file = sources[channels_map["G"]]  # type: ignore[index]
    b_file = sources[channels_map["B"]]  # type: ignore[index]
    cmds.append(
        f"rgbcomp {r_file} {g_file} {b_file} -out={_quote(out_image.resolve())}"
    )
    return cmds
