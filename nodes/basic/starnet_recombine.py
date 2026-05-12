"""starnet_recombine: glue the starless + stars layers back into one image.

Inputs : starless (IMAGE_FITS), stars (IMAGE_FITS)
Output : image    (IMAGE_FITS) - element-wise sum of the two layers

This is the closing node of the StarNet trio. Default `enabled=True` so a
pipeline whose extract+replace are off naturally reduces to identity:
  starless = original
  stars    = zero image
  combined = original + 0 = original

When extract is on but recombine is off (rare) we pass through the
starless layer; that's a useful escape hatch for "produce a starless
image" workflows.

Pure numpy. Header from the starless input is preserved (it's the more
load-bearing one, since starless typically inherits the WCS / metadata
from the upstream stretch).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class StarnetRecombineParams(BaseModel):
    enabled: bool = Field(
        default=True,
        description="On by default. Off makes the node forward the starless "
        "layer as the final image, which is useful when you want a "
        "starless-only render.",
        json_schema_extra={
            "agent_hint": (
                "Disable to export a starless image for background-only"
                " compositing or social media."
            ),
        },
    )
    blend: Literal["add", "screen", "max"] = Field(
        default="add",
        description="How to combine starless and stars. 'add' (default) is "
        "the inverse of how StarNet split them and gives a faithful "
        "recompose. 'screen' (1 - (1-a)*(1-b)) flatters bright stars "
        "without blowing them out; 'max' is a hard pick-the-brightest "
        "useful when stars and nebulosity overlap and you want stars to "
        "dominate.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Use 'add' for a natural recompose; 'screen' tames blown-out"
                " star cores; 'max' makes stars pop against dim nebulosity."
            ),
        },
    )


@register("starnet_recombine")
class StarnetRecombineNode(Node[StarnetRecombineParams]):
    id = "starnet_recombine"
    version = 1
    cost = "cheap"
    preview_display_ready = True

    inputs = {
        "starless": PortType.IMAGE_FITS,
        "stars": PortType.IMAGE_FITS,
    }
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = StarnetRecombineParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: StarnetRecombineParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        starless_src = inputs["starless"].path
        stars_src = inputs["stars"].path
        if not starless_src.exists():
            raise RuntimeError(f"starnet_recombine: starless input does not exist: {starless_src}")
        if not stars_src.exists():
            raise RuntimeError(f"starnet_recombine: stars input does not exist: {stars_src}")

        out_image = out_dir_path / "image.fit"

        ctx.progress(0.1, "starnet_recombine: reading layers")
        starless_data, header = _read_fits(starless_src)

        if not params.enabled:
            ctx.progress(0.9, "starnet_recombine: disabled — passing starless")
            fits.PrimaryHDU(data=starless_data, header=header).writeto(out_image, overwrite=True)
            ctx.progress(1.0, "starnet_recombine: pass-through")
            return _result(out_image)

        stars_data, _ = _read_fits(stars_src)
        if starless_data.shape != stars_data.shape:
            raise RuntimeError(
                f"starnet_recombine: shape mismatch starless={starless_data.shape} "
                f"stars={stars_data.shape}"
            )

        ctx.progress(0.5, f"starnet_recombine: blend={params.blend}")
        a = starless_data.astype(np.float32)
        b = stars_data.astype(np.float32)
        if params.blend == "add":
            combined = a + b
        elif params.blend == "screen":
            # Standard screen blend in [0,1]: 1 - (1-a)*(1-b). Clamp inputs
            # so a stretched float that sits outside [0,1] (it shouldn't,
            # post-stretch, but defensive) doesn't blow up the formula.
            ac = np.clip(a, 0.0, 1.0)
            bc = np.clip(b, 0.0, 1.0)
            combined = 1.0 - (1.0 - ac) * (1.0 - bc)
        else:  # 'max'
            combined = np.maximum(a, b)

        # Stretch nodes upstream emit float32 in [0, 1]; we don't clamp here
        # for 'add' so a bit of head-room over 1 survives if the stars layer
        # has hot pixels. save_image / preview will clamp at the end.
        combined = combined.astype(starless_data.dtype, copy=False)

        fits.PrimaryHDU(data=combined, header=header).writeto(out_image, overwrite=True)
        ctx.progress(1.0, "starnet_recombine: done")
        return _result(out_image)


def _read_fits(src: Path) -> tuple[np.ndarray, fits.Header]:
    with fits.open(src, memmap=False) as hdul:
        data = hdul[0].data
        header = hdul[0].header.copy()
        if data is None:
            for ext in hdul[1:]:
                if ext.data is not None:
                    data = ext.data
                    header = ext.header.copy()
                    break
    if data is None:
        raise RuntimeError(f"starnet_recombine: no image data in {src}")
    return data, header


def _result(out_image: Path) -> dict[str, Ref]:
    return {
        "image": Ref(
            node_hash="",
            port="image",
            path=out_image,
            type=PortType.IMAGE_FITS,
            display_ready=True,
        )
    }
