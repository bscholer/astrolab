"""save_image: render a stretched FITS to a viewable PNG.

Input port  : image (IMAGE_FITS) - typically the output of stretch
Output port : image (IMAGE_PNG)  - 16-bit PNG ready to download/share

This is the terminal node of the canned pipeline. The pixel data should
already be in [0,1] (post-stretch); we just hand it to Siril's `savepng`
which writes a 16-bit PNG with the current display state baked in. JPEG
output can land later if file-size pressure shows up.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from nodes.base import Node
from nodes.basic.calibrate import _quote
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class SaveImageParams(BaseModel):
    """No params yet. Format/quality knobs land when we have a use case for them."""


@register("save_image")
class SaveImageNode(Node[SaveImageParams]):
    id = "save_image"
    version = 1
    cost = "cheap"

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_PNG}
    params_schema = SaveImageParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: SaveImageParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path

        if not src.exists():
            raise RuntimeError(f"save_image: input image does not exist: {src}")

        out_png = out_dir_path / "image.png"
        # Siril's `savepng` appends .png itself, so pass the stem.
        save_stem = out_png.with_suffix("").name

        ctx.progress(0.2, "save_image: rendering PNG")
        commands = [
            f"cd {_quote(out_dir_path.resolve())}",
            f"load {_quote(src.resolve())}",
            f"savepng {_quote(save_stem)}",
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
                f"save_image: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        if not out_png.exists():
            raise RuntimeError(
                f"save_image: siril returned 0 but {out_png} is missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        ctx.progress(1.0, f"save_image: wrote {out_png.name}")
        return {
            "image": Ref(
                node_hash="",
                port="image",
                path=out_png,
                type=PortType.IMAGE_PNG,
            )
        }
