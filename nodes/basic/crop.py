"""crop: rectangular crop with normalized coords, driven by the UI picker.

Input port  : image (IMAGE_FITS) - typically the output of stretch
Output port : image (IMAGE_FITS) - same FITS, sliced to the requested rect

Coordinates are stored in [0, 1] (normalized to the input frame's spatial
dims) so the dragged rectangle in the UI stays valid even after upstream
nodes change the resolution (e.g. flipping resample mode draft <-> full).
The runtime maps normalized -> pixels at run time.

Disabled by default — the node only kicks in once the user actually drags a
box in the UI and toggles `enabled` on. While disabled, run() pass-throughs
the input so downstream hashes remain stable per-run.

Pure numpy/astropy; same WCS-shift treatment as auto_crop.
"""

from __future__ import annotations

from pathlib import Path

from astropy.io import fits
from pydantic import BaseModel, Field, model_validator

from nodes.base import Node
from nodes.basic.auto_crop import _slice_spatial, _spatial_shape
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class CropParams(BaseModel):
    enabled: bool = Field(
        default=False,
        description="When off the node passes through the input unchanged. "
        "The interactive crop picker in the UI flips this on once you drag "
        "a rectangle.",
    )
    x: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Left edge of the crop rectangle, normalized to the "
        "input image width (0 = left edge, 1 = right edge).",
        json_schema_extra={"hash_precision": 4, "ui_hidden": True},
    )
    y: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Top edge of the crop rectangle, normalized to the input "
        "image height (0 = top, 1 = bottom).",
        json_schema_extra={"hash_precision": 4, "ui_hidden": True},
    )
    width: float = Field(
        default=1.0,
        gt=0.0,
        le=1.0,
        description="Width of the crop rectangle, normalized to the input "
        "image width.",
        json_schema_extra={"hash_precision": 4, "ui_hidden": True},
    )
    height: float = Field(
        default=1.0,
        gt=0.0,
        le=1.0,
        description="Height of the crop rectangle, normalized to the input "
        "image height.",
        json_schema_extra={"hash_precision": 4, "ui_hidden": True},
    )

    @model_validator(mode="after")
    def _check_box(self) -> "CropParams":
        # Cap width/height to whatever's left of the frame given x/y so the
        # UI doesn't have to clamp on every drag.
        if self.x + self.width > 1.0 + 1e-6:
            object.__setattr__(self, "width", max(1e-3, 1.0 - self.x))
        if self.y + self.height > 1.0 + 1e-6:
            object.__setattr__(self, "height", max(1e-3, 1.0 - self.y))
        return self


@register("crop")
class CropNode(Node[CropParams]):
    id = "crop"
    version = 1
    cost = "cheap"

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = CropParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: CropParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path
        if not src.exists():
            raise RuntimeError(f"crop: input image does not exist: {src}")

        out_image = out_dir_path / "image.fit"

        ctx.progress(0.1, "crop: reading FITS")
        with fits.open(src, memmap=False) as hdul:
            hdu = hdul[0]
            data = hdu.data
            header = hdu.header.copy()
            if data is None:
                for ext in hdul[1:]:
                    if ext.data is not None:
                        data = ext.data
                        header = ext.header.copy()
                        break
        if data is None:
            raise RuntimeError(f"crop: no image data in {src}")

        if not params.enabled:
            ctx.progress(0.9, "crop: disabled — passing through")
            fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
            ctx.progress(1.0, "crop: wrote pass-through")
            return _result(out_image)

        full_h, full_w = _spatial_shape(data)
        x = int(round(params.x * full_w))
        y = int(round(params.y * full_h))
        w = int(round(params.width * full_w))
        h = int(round(params.height * full_h))

        # Clamp into bounds; drop to a 1-pixel min so we never produce a
        # zero-area FITS (which would crash downstream stretch/save).
        x = max(0, min(full_w - 1, x))
        y = max(0, min(full_h - 1, y))
        w = max(1, min(full_w - x, w))
        h = max(1, min(full_h - y, h))

        if x == 0 and y == 0 and w == full_w and h == full_h:
            ctx.progress(0.9, "crop: full-frame box — passing through")
            fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
            ctx.progress(1.0, "crop: done")
            return _result(out_image)

        cropped = _slice_spatial(data, x, y, w, h)

        for key, off in (("CRPIX1", x), ("CRPIX2", y)):
            if key in header:
                try:
                    header[key] = float(header[key]) - off
                except (TypeError, ValueError):
                    pass

        ctx.progress(0.9, f"crop: {full_w}x{full_h} -> {w}x{h}")
        fits.PrimaryHDU(data=cropped, header=header).writeto(out_image, overwrite=True)
        ctx.progress(1.0, "crop: done")
        return _result(out_image)


def _result(out_image: Path) -> dict[str, Ref]:
    return {
        "image": Ref(
            node_hash="",
            port="image",
            path=out_image,
            type=PortType.IMAGE_FITS,
        )
    }
