"""auto_crop: trim the nodata border left by registration.

Input port  : image (IMAGE_FITS) - typically the output of seq_stack
Output port : image (IMAGE_FITS) - same FITS, cropped to the bbox of real data

After registration, frames are warped onto the reference grid and any pixel
that fell outside the original frame is filled with zeros. The stacked output
inherits those zero edges; downstream stretch+save end up reading a frame
that's largely empty noise around the real signal.

This node finds the bounding box of pixels above a small threshold (the
pedestal pushes real data ~0.01 above zero, so a 0.001 cutoff cleanly
separates "registered nodata" from "real signal") and slices to that box.
Pure numpy/astropy — no Siril round-trip — keeps it cheap.

WCS keys CRPIX1/CRPIX2 (if present) are shifted so a future plate-solve on
the cropped frame stays consistent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class AutoCropParams(BaseModel):
    enabled: bool = Field(
        default=True,
        description="When off the node passes the input through unchanged. "
        "Default on — trimming the registration nodata border is almost always "
        "wanted and shrinks the data that stretch + save have to chew through.",
    )
    threshold: float = Field(
        default=1e-3,
        ge=0.0,
        le=1.0,
        description="Pixels at or below this value count as nodata when "
        "computing the bounding box. The pedestal_offset step pushes real "
        "signal up by ~0.01, so values around 0.001 reliably catch the "
        "registration's zero-fill edge without trimming faint real data.",
        json_schema_extra={"hash_precision": 6, "ui_section": "advanced"},
    )
    padding: int = Field(
        default=0,
        ge=0,
        le=512,
        description="Extra pixels of margin to keep around the detected bbox. "
        "Useful when the threshold trims slightly into stars at the frame "
        "edge; bump up by a few px to recover them.",
        json_schema_extra={"ui_section": "advanced"},
    )


@register("auto_crop")
class AutoCropNode(Node[AutoCropParams]):
    id = "auto_crop"
    version = 1
    cost = "cheap"

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = AutoCropParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: AutoCropParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path
        if not src.exists():
            raise RuntimeError(f"auto_crop: input image does not exist: {src}")

        out_image = out_dir_path / "image.fit"

        ctx.progress(0.1, "auto_crop: reading FITS")
        with fits.open(src, memmap=False) as hdul:
            hdu = hdul[0]
            data = hdu.data
            header = hdu.header.copy()
            if data is None:
                # Some pipelines drop the primary HDU's data; pick the first
                # extension that has any.
                for ext in hdul[1:]:
                    if ext.data is not None:
                        data = ext.data
                        header = ext.header.copy()
                        break
        if data is None:
            raise RuntimeError(f"auto_crop: no image data in {src}")

        if not params.enabled:
            ctx.progress(0.9, "auto_crop: disabled — passing through")
            fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
            ctx.progress(1.0, "auto_crop: wrote pass-through")
            return _result(out_image)

        x, y, w, h = _bbox_of_signal(data, params.threshold)
        if w <= 0 or h <= 0:
            # Nothing above threshold — keep the original rather than emitting
            # a zero-area FITS that would crash downstream.
            ctx.progress(0.9, "auto_crop: no signal detected, passing through")
            fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
            ctx.progress(1.0, "auto_crop: wrote pass-through")
            return _result(out_image)

        full_h, full_w = _spatial_shape(data)
        if params.padding > 0:
            pad = params.padding
            x = max(0, x - pad)
            y = max(0, y - pad)
            w = min(full_w - x, w + 2 * pad)
            h = min(full_h - y, h + 2 * pad)

        if x == 0 and y == 0 and w == full_w and h == full_h:
            ctx.progress(0.9, f"auto_crop: nothing to trim ({full_w}x{full_h})")
            fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
            ctx.progress(1.0, "auto_crop: wrote pass-through")
            return _result(out_image)

        cropped = _slice_spatial(data, x, y, w, h)

        # Shift WCS reference pixel so plate-solve metadata stays consistent.
        # 1-based FITS convention: CRPIX1/2 reference the (1,1)-origin pixel.
        for key, off in (("CRPIX1", x), ("CRPIX2", y)):
            if key in header:
                try:
                    header[key] = float(header[key]) - off
                except (TypeError, ValueError):
                    pass

        ctx.progress(
            0.9,
            f"auto_crop: {full_w}x{full_h} -> {w}x{h} "
            f"(trimmed {full_w - w}x{full_h - h})",
        )
        fits.PrimaryHDU(data=cropped, header=header).writeto(out_image, overwrite=True)
        ctx.progress(1.0, "auto_crop: done")
        return _result(out_image)


def _spatial_shape(arr: np.ndarray) -> tuple[int, int]:
    """Return (height, width) of the spatial axes regardless of channel layout."""
    if arr.ndim == 2:
        return int(arr.shape[0]), int(arr.shape[1])
    if arr.ndim == 3 and arr.shape[0] in (3, 4):
        return int(arr.shape[1]), int(arr.shape[2])
    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        return int(arr.shape[0]), int(arr.shape[1])
    raise RuntimeError(f"auto_crop: unsupported FITS shape {arr.shape}")


def _slice_spatial(arr: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    if arr.ndim == 2:
        return arr[y : y + h, x : x + w]
    if arr.ndim == 3 and arr.shape[0] in (3, 4):
        return arr[:, y : y + h, x : x + w]
    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        return arr[y : y + h, x : x + w, :]
    raise RuntimeError(f"auto_crop: unsupported FITS shape {arr.shape}")


def _bbox_of_signal(arr: np.ndarray, threshold: float) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) of pixels with any channel > threshold.

    Per-axis any() reduces RGB cubes to a 2D mask where a pixel counts as
    "real" if any channel is above threshold — preserves edge content where
    only one channel happens to be lit (rare in practice but cheap to handle).
    """
    finite = np.isfinite(arr)
    above = finite & (arr > threshold)
    if arr.ndim == 2:
        mask = above
    elif arr.ndim == 3 and arr.shape[0] in (3, 4):
        mask = above.any(axis=0)
    elif arr.ndim == 3 and arr.shape[-1] in (3, 4):
        mask = above.any(axis=-1)
    else:
        raise RuntimeError(f"auto_crop: unsupported FITS shape {arr.shape}")

    if not mask.any():
        return 0, 0, 0, 0

    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    y, ymax = int(rows[0]), int(rows[-1])
    x, xmax = int(cols[0]), int(cols[-1])
    return x, y, xmax - x + 1, ymax - y + 1


def _result(out_image: Path) -> dict[str, Ref]:
    return {
        "image": Ref(
            node_hash="",
            port="image",
            path=out_image,
            type=PortType.IMAGE_FITS,
        )
    }
