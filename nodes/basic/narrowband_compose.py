"""narrowband_compose: PixelMath OIII normalization + palette rgbcomp.

Input ports : ha   (IMAGE_FITS) - aligned Ha stack from narrowband_extract
              oiii (IMAGE_FITS) - aligned OIII stack from narrowband_extract
Output port : image (IMAGE_FITS) - composed RGB at out_dir/image.fit

Phase 2 of the OSC narrowband flow. Cheap; tweaking the palette only
re-runs this node, leaving the heavy register+stack work in
narrowband_extract cached.

The Siril chain is the second half of Naztronomy's Smart-Telescope-PP
narrowband flow, copied verbatim because that script is the working
ground truth for the OIII normalization formula:

    pm <OIII normalized to Ha statistics>     -> normalized_r_results_oiii.fit
    [HSO only] pm (Ha * 0.7) + (OIII * 0.3)   -> synthetic_green.fit
    rgbcomp <R> <G> <B> -out=image            per PALETTE_CONFIG.

PALETTE_CONFIG is shared verbatim from the Naztronomy script; palette
ideas come from
https://www.cloudynights.com/topic/800240-differences-of-bi-color-pallets-ha-oiii/.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nodes._seq_runner import image_ref, quote
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler

# Palette configuration: which extracted channel feeds each RGB output.
# Copied verbatim from Naztronomy's Smart-Telescope-PP.py; palette ideas from
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


class NarrowbandComposeParams(BaseModel):
    mode: PaletteMode = Field(
        default="hoo",
        description="Bi-color or tri-color palette to render. 'hoo' is the "
        "traditional Ha-as-red, OIII-as-cyan composition that most OSC "
        "narrowband shooters expect. 'hso' adds a synthetic-green channel for "
        "a Hubble-like tone.",
        json_schema_extra={
            "agent_hint": (
                "Choose 'hoo' for a classic red-nebula/cyan-shell look, 'hso' for"
                " a Hubble gold-and-blue palette, or experiment with 'oho'/'hho'"
                " for unusual color emphasis."
            ),
        },
    )


@register("narrowband_compose")
class NarrowbandComposeNode(Node[NarrowbandComposeParams]):
    id = "narrowband_compose"
    version = 1
    cost = "cheap"
    uses_siril = True

    inputs = {
        "ha": PortType.IMAGE_FITS,
        "oiii": PortType.IMAGE_FITS,
    }
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = NarrowbandComposeParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: NarrowbandComposeParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        ha_in = inputs["ha"].path
        oiii_in = inputs["oiii"].path

        if not ha_in.exists():
            raise RuntimeError(
                f"narrowband_compose: ha input does not exist: {ha_in}"
            )
        if not oiii_in.exists():
            raise RuntimeError(
                f"narrowband_compose: oiii input does not exist: {oiii_in}"
            )

        # Siril's PixelMath references identifiers by filename. Stage the two
        # inputs under r_results_ha.fit + r_results_oiii.fit so the formula
        # (verbatim from Naztronomy) keeps its semantic identifiers.
        work_dir = out_dir_path / "_compose"
        work_dir.mkdir(parents=True, exist_ok=True)
        ha_staged = work_dir / "r_results_ha.fit"
        oiii_staged = work_dir / "r_results_oiii.fit"
        _link_or_copy(ha_in, ha_staged)
        _link_or_copy(oiii_in, oiii_staged)

        out_image = out_dir_path / "image.fit"
        commands = _build_compose_commands(params.mode, work_dir, out_image)

        ctx.progress(0.1, f"narrowband_compose: composing {params.mode.upper()}")
        runtime = SirilRuntime()
        # Progress-emitting Siril ops: pm normalize, [pm synthetic for HSO],
        # rgbcomp. phases must match the exact count for this mode, otherwise
        # the bar rewinds when the next sub-command opens at 0%.
        n_phases = (
            3 if PALETTE_CONFIG[params.mode].get("requires_synthetic") else 2
        )
        result = runtime.run(
            commands,
            working_dir=work_dir,
            on_log=make_progress_handler(ctx, phases=n_phases),
            cancel=ctx.cancel,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"narrowband_compose: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        if not out_image.exists():
            raise RuntimeError(
                f"narrowband_compose: siril returned 0 but {out_image} is "
                f"missing.\n--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        with contextlib.suppress(OSError):
            shutil.rmtree(work_dir)

        ctx.progress(1.0, f"narrowband_compose: wrote {out_image.name}")
        return {"image": image_ref(out_image)}


def _build_compose_commands(
    mode: str, work_dir: Path, out_image: Path
) -> list[str]:
    """Render the Siril SSF for the PixelMath+rgbcomp half of the narrowband flow.

    Kept as a free function so tests can introspect the command list without
    spinning up SirilRuntime. Formulas are verbatim from Naztronomy.
    """
    config = PALETTE_CONFIG[mode]
    channels_map = config["channels"]  # type: ignore[index]
    requires_synthetic = bool(config.get("requires_synthetic", False))

    ha_aligned = "r_results_ha"
    oiii_aligned = "r_results_oiii"
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

    cmds: list[str] = [
        f"cd {quote(work_dir.resolve())}",
        # Load the registered OIII stack and normalize it to Ha's intensity
        # statistics. Save the result so rgbcomp can reference it by name.
        f"load {oiii_aligned}",
        f"pm {normalize_formula}",
        f"save {normalized_oiii}",
    ]

    # HSO only: synthesize a green channel as 0.7*Ha + 0.3*OIII so rgbcomp
    # can place it in G.
    if requires_synthetic:
        cmds.append(f"pm {synthetic_formula}")
        cmds.append(f"save {synthetic_green}")

    sources = {
        "ha": ha_aligned,
        "oiii": normalized_oiii,
        "synthetic": synthetic_green,
    }
    r_file = sources[channels_map["R"]]  # type: ignore[index]
    g_file = sources[channels_map["G"]]  # type: ignore[index]
    b_file = sources[channels_map["B"]]  # type: ignore[index]
    cmds.append(
        f"rgbcomp {r_file} {g_file} {b_file} -out={quote(out_image.resolve())}"
    )
    return cmds


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink src into dst; fall back to copy across filesystems.

    Avoids duplicating the (possibly large) aligned stacks just to feed them
    into Siril's CWD-based identifier resolution.
    """
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy(src, dst)
