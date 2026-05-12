"""Trivial no-Siril node used to exercise the runner end-to-end.

Reads a PNG, downscales the long edge to `target_size_px`, writes a PNG. No
Siril dependency, runs anywhere Pillow runs.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class DownscaleParams(BaseModel):
    target_size_px: int = Field(
        default=1024,
        gt=0,
        le=8192,
        description="Long edge of the output image, in pixels.",
        json_schema_extra={
            "agent_hint": (
                "Lower produces a smaller, faster-loading thumbnail;"
                " higher retains more fine detail for sharing."
            ),
        },
    )


@register("downscale")
class DownscaleNode(Node[DownscaleParams]):
    id = "downscale"
    version = 1
    cost = "cheap"

    inputs = {"image": PortType.IMAGE_PNG}
    outputs = {"image": PortType.IMAGE_PNG}
    params_schema = DownscaleParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: DownscaleParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"]
        ctx.progress(0.0, f"downscale: reading {src.path.name}")

        with Image.open(src.path) as img:
            img.load()
            w, h = img.size
            long_edge = max(w, h)
            scale = (
                1.0 if long_edge <= params.target_size_px else params.target_size_px / long_edge
            )
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            ctx.progress(0.5, f"downscale: {w}x{h} -> {new_size[0]}x{new_size[1]}")
            resized = img.resize(new_size, Image.Resampling.LANCZOS)
            out_path = out_dir_path / "image.png"
            resized.save(out_path, format="PNG")

        ctx.progress(1.0, "downscale: done")
        # node_hash is patched in by the runner; nodes don't compute their own hash.
        return {
            "image": Ref(
                node_hash="",
                port="image",
                path=out_path,
                type=PortType.IMAGE_PNG,
            )
        }
