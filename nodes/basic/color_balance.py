"""color_balance: SCNR green-cast removal for OSC stacks.

Input port  : image (IMAGE_FITS) - typically the output of crop / graxpert
Output port : image (IMAGE_FITS) - green-neutralized FITS for auto_bp_shift / stretch

OSC sensors (Dwarf 3, ZWO, etc.) ship with a green-heavy CFA pattern: the
Bayer matrix has twice as many green photosites as red or blue. Even after
calibration, registration, and a rejection stack with -rgb_equal, broadband
targets like the Veil Nebula come out with a strong green wash that does
not match the visual or photographic truth of the field.

Siril's `rmgreen` (SCNR green) is the standard fix: for each pixel, pull
green down toward the max of red and blue (average-neutral protection
mode). On a clean stack this removes the green cast without crushing
genuinely green-rich regions, because the protection rule never lets G
exceed the local R/B envelope by more than 'amount' allows.

Runs in the linear domain, before auto_bp_shift / stretch. SCNR is
luminance-preserving, so applying it pre-stretch lets the autostretch
midtone curve work on color-neutral data and avoids amplifying the green
cast into a permanent stained look post-stretch.

Defaults to enabled=True because every OSC chain we've shipped has shown
the green cast and SCNR is the canonical first move. Disable to compare
the raw rejected stack or when working on a target where green is real
(e.g. Wolf-Rayet emission shells).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from nodes._seq_runner import image_ref, quote
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class ColorBalanceParams(BaseModel):
    enabled: bool = Field(
        default=True,
        description="When off the node passes the input through unchanged. "
        "Default on for OSC stacks: the Bayer matrix's 2x-green oversample "
        "leaves a visible green cast on every broadband target, and SCNR is "
        "the canonical first-step fix.",
        json_schema_extra={
            "agent_hint": (
                "Disable to compare the raw stack, or for targets where green"
                " emission is genuinely present (Wolf-Rayet shells, some PNe)."
            ),
        },
    )
    amount: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="SCNR strength in [0, 1]. 1.0 fully clips green above the "
        "local R/B envelope; 0.0 is a no-op. 0.7-0.9 is the practical range "
        "for OSC smart-telescope stacks: enough to kill the cast without "
        "draining color from faint nebulosity.",
        json_schema_extra={
            "hash_precision": 2,
            "agent_hint": (
                "Higher = more aggressive green removal."
                " Above 0.95 can flatten faint green-yellow nebula tones."
            ),
        },
    )


@register("color_balance")
class ColorBalanceNode(Node[ColorBalanceParams]):
    id = "color_balance"
    version = 1
    cost = "cheap"
    uses_siril = True

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = ColorBalanceParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: ColorBalanceParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path

        if not src.exists():
            raise RuntimeError(f"color_balance: input image does not exist: {src}")

        out_image = out_dir_path / "image.fit"

        if not params.enabled:
            ctx.progress(0.5, "color_balance: disabled - passing through")
            shutil.copy(src, out_image)
            ctx.progress(1.0, "color_balance: wrote pass-through")
            return {"image": image_ref(out_image)}

        # Siril's `save` writes a FITS by default; it appends .fit so we
        # strip the suffix from the stem.
        save_stem = out_image.with_suffix("").name

        ctx.progress(0.2, f"color_balance: SCNR green amount={params.amount:g}")
        # rmgreen <type> <amount> -- type 0 = average-neutral protection,
        # the only mode that makes sense for OSC broadband stacks.
        commands = [
            f"cd {quote(out_dir_path.resolve())}",
            f"load {quote(src.resolve())}",
            f"rmgreen 0 {params.amount:g}",
            f"save {quote(save_stem)}",
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=out_dir_path,
            on_log=make_progress_handler(ctx),
            cancel=ctx.cancel,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"color_balance: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        if not out_image.exists():
            raise RuntimeError(
                f"color_balance: siril returned 0 but {out_image} is missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        ctx.progress(1.0, f"color_balance: wrote {out_image.name}")
        return {"image": image_ref(out_image)}
