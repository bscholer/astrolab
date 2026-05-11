"""rgb_equal: equalize RGB channel means after stacking.

Input port  : image (IMAGE_FITS) - typically the output of auto_bp_shift
Output port : image (IMAGE_FITS) - channel-equalized FITS for the stretch node

Wraps Siril's `rgb_equal` command, which rescales the red and blue channels
so their mean matches the green channel mean. This is a purely linear
operation and leaves the luminance distribution of the green channel
(usually dominant in OSC Bayer data) untouched.

Why a second rgb_equal pass here:
  seq_stack already runs -rgb_equal during stacking, but the output_norm step
  that follows clamps pixel values to [0, 1], which can slightly shift channel
  means and reintroduce a faint green bias. Running rgb_equal again as a
  standalone node, last in the linear pipeline right before stretch, ensures
  the tone curve operates on balanced channels.

This node is a pre-stretch operation. Its output is linear (no tone mapping
applied) and should be followed by a stretch node.
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


class RgbEqualParams(BaseModel):
    enabled: bool = Field(
        default=True,
        description="When off the node passes the input through unchanged. "
        "Default on: a second rgb_equal pass corrects any residual green bias "
        "that survives output_norm clipping in seq_stack, ensuring the stretch "
        "node sees balanced channels.",
    )


@register("rgb_equal")
class RgbEqualNode(Node[RgbEqualParams]):
    id = "rgb_equal"
    version = 1
    cost = "cheap"
    uses_siril = True

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = RgbEqualParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: RgbEqualParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path

        if not src.exists():
            raise RuntimeError(f"rgb_equal: input image does not exist: {src}")

        out_image = out_dir_path / "image.fit"

        if not params.enabled:
            ctx.progress(0.5, "rgb_equal: disabled - passing through")
            shutil.copy(src, out_image)
            ctx.progress(1.0, "rgb_equal: wrote pass-through")
            return {"image": image_ref(out_image)}

        # Siril's `save` writes a FITS by default; it appends .fit so we
        # strip the suffix from the stem.
        save_stem = out_image.with_suffix("").name

        commands = [
            f"cd {quote(out_dir_path.resolve())}",
            f"load {quote(src.resolve())}",
            "rgb_equal",
            f"save {save_stem}",
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
                f"rgb_equal: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        if not out_image.exists():
            raise RuntimeError(
                f"rgb_equal: siril returned 0 but {out_image} is missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        ctx.progress(1.0, f"rgb_equal: wrote {out_image.name}")
        return {"image": image_ref(out_image)}
