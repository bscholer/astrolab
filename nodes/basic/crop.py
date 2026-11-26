"""crop: frame the picture - a box on the sky, any angle, or the empty border trimmed.

Input port  : image (IMAGE_FITS) - the combined stack (placed right after it,
              before GraXpert, color and the stretch, so they measure only
              what is framed).
Output port : image (IMAGE_FITS) - the framed image, WCS carried over.

Three modes, first match wins
-----------------------------
Sky box (sky_width > 0)
    Centre RA/Dec, width and height in arcminutes, and the position angle
    of the picture's up direction (0 = north up). The node maps it onto
    this canvas through the plate solve, so the framing stays on the same
    sky when a new night grows or shifts the combined canvas. An image
    without a plate solve fails loudly rather than guessing.

Canvas box (width > 0)
    x/y/width/height in [0,1] of the image as displayed (Siril's
    convention, FITS row 0 at the bottom: y is measured from the top of
    the preview) and `rotation` in degrees clockwise. For images with no
    plate solve.

Auto-trim (neither set)
    Only rows and columns with no data at all are cut (pixels at or below
    `threshold`); partly covered edges are real data you frame yourself.

Rotated boxes are resampled with cubic convolution; straight boxes on whole
pixels are sliced. Area outside the canvas comes out as 0 (nodata).

Pure numpy/astropy; no Siril round-trip.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field, model_validator

from nodes.base import Node
from nodes.basic.framing import CanvasBox, SkyBox, celestial_wcs, cut, cut_header, sky_to_canvas
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class CropParams(BaseModel):
    enabled: bool = Field(
        default=True,
        description="When off the node passes through the input unchanged. "
        "When on and no explicit box has been set (width == 0), the node "
        "auto-detects the bounding box of real signal using the threshold "
        "below. Once the user drags a rectangle in the UI the explicit "
        "coords take over.",
        json_schema_extra={
            "agent_hint": (
                "Leave on to auto-trim the registration nodata border. "
                "Enable with a tight box (via the UI picker) to also remove "
                "distracting field edges or center the subject for a stronger "
                "composition."
            ),
        },
    )
    x: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Left edge of the crop rectangle, normalized to the "
        "input image width (0 = left edge, 1 = right edge). "
        "Ignored when width == 0 (auto-trim mode).",
        json_schema_extra={"hash_precision": 4, "ui_hidden": True},
    )
    y: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Top edge of the crop rectangle, normalized to the input "
        "image height as displayed (0 = top of the preview, FITS row 0 at the "
        "bottom). "
        "Ignored when height == 0 (auto-trim mode).",
        json_schema_extra={"hash_precision": 4, "ui_hidden": True},
    )
    width: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Width of the crop rectangle, normalized to the input "
        "image width. 0 is the sentinel meaning 'no user box set yet — "
        "auto-detect the signal bounding box instead'.",
        json_schema_extra={"hash_precision": 4, "ui_hidden": True},
    )
    height: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Height of the crop rectangle, normalized to the input "
        "image height. 0 is the sentinel matching width == 0 for auto-trim.",
        json_schema_extra={"hash_precision": 4, "ui_hidden": True},
    )
    rotation: float = Field(
        default=0.0,
        ge=-360.0,
        le=360.0,
        description="Turn of the canvas box, degrees clockwise on the "
        "preview, about its centre.",
        json_schema_extra={"hash_precision": 3, "ui_hidden": True},
    )
    ra: float = Field(
        default=0.0,
        ge=0.0,
        le=360.0,
        description="Sky box: right ascension of the centre, degrees.",
        json_schema_extra={"hash_precision": 6, "ui_hidden": True},
    )
    dec: float = Field(
        default=0.0,
        ge=-90.0,
        le=90.0,
        description="Sky box: declination of the centre, degrees.",
        json_schema_extra={"hash_precision": 6, "ui_hidden": True},
    )
    sky_width: float = Field(
        default=0.0,
        ge=0.0,
        description="Sky box: width in arcminutes. 0 means no sky box.",
        json_schema_extra={"hash_precision": 3, "ui_hidden": True},
    )
    sky_height: float = Field(
        default=0.0,
        ge=0.0,
        description="Sky box: height in arcminutes.",
        json_schema_extra={"hash_precision": 3, "ui_hidden": True},
    )
    angle: float = Field(
        default=0.0,
        ge=-360.0,
        le=360.0,
        description="Sky box: position angle of the picture's up direction, "
        "degrees east of north (0 = north up, 90 = east up).",
        json_schema_extra={"hash_precision": 3, "ui_hidden": True},
    )
    threshold: float = Field(
        default=1e-3,
        ge=0.0,
        le=1.0,
        description="Pixels at or below this value count as nodata when "
        "auto-computing the bounding box (auto-trim mode only). The "
        "pedestal_offset step pushes real signal up by ~0.01, so values "
        "around 0.001 reliably catch the registration's zero-fill edge "
        "without trimming faint real data.",
        json_schema_extra={
            "hash_precision": 6,
            "ui_section": "advanced",
            "agent_hint": (
                "Lower keeps more edge pixels; higher trims more aggressively"
                " and may clip faint stars at the frame boundary."
            ),
        },
    )
    padding: int = Field(
        default=0,
        ge=0,
        le=512,
        description="Extra pixels of margin to keep around the auto-detected "
        "bbox (auto-trim mode only). Useful when the threshold trims slightly "
        "into stars at the frame edge; bump up by a few px to recover them.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Increase by 10-50 px if the auto-trim is cutting into real "
                "stars or nebulosity at the frame edge."
            ),
        },
    )

    @model_validator(mode="after")
    def _check_box(self) -> CropParams:
        # Only clamp a straight box the user set (width > 0); a turned box
        # may reach past the canvas corners (that area comes out empty).
        if self.width > 0 and self.rotation % 360 == 0:
            if self.x + self.width > 1.0 + 1e-6:
                object.__setattr__(self, "width", max(1e-3, 1.0 - self.x))
            if self.y + self.height > 1.0 + 1e-6:
                object.__setattr__(self, "height", max(1e-3, 1.0 - self.y))
        return self


@register("crop")
class CropNode(Node[CropParams]):
    id = "crop"
    version = 5
    tier = "keep"

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

        if params.sky_width > 0 and params.sky_height > 0:
            wcs = celestial_wcs(header)
            if wcs is None:
                raise RuntimeError(
                    "crop: the framing is set on the sky, but this image has no "
                    "plate solve to place it with"
                )
            box = sky_to_canvas(
                wcs,
                SkyBox(params.ra, params.dec, params.sky_width, params.sky_height, params.angle),
            )
            what = (
                f"sky box {params.sky_width:.0f}'x{params.sky_height:.0f}' "
                f"at PA {params.angle:.0f}"
            )
        elif params.width > 0:
            box = CanvasBox(
                (params.x + params.width / 2) * full_w,
                full_h - (params.y + params.height / 2) * full_h,
                params.width * full_w,
                params.height * full_h,
                -math.radians(params.rotation),
            )
            what = "user box"
        else:
            # Auto-trim: only rows and columns with no data at all go.
            # Offset nights and mosaics leave partly covered edges, and those
            # are real data the user frames (or not) themselves.
            ctx.progress(0.3, "crop: auto-detecting signal bbox")
            x, y, w, h = _bbox_of_signal(data, params.threshold)
            if w <= 0 or h <= 0:
                ctx.progress(0.9, "crop: no signal detected, passing through")
                return _write(out_image, data, header)
            if params.padding > 0:
                pad = params.padding
                x, y = max(0, x - pad), max(0, y - pad)
                w, h = min(full_w - x, w + 2 * pad), min(full_h - y, h + 2 * pad)
            box = CanvasBox(x + w / 2, y + h / 2, w, h)
            what = "auto-trim"

        out_w, out_h = max(1, round(box.width)), max(1, round(box.height))
        full = CanvasBox(full_w / 2, full_h / 2, full_w, full_h)
        if (
            abs(math.sin(box.theta)) < 1e-9
            and math.cos(box.theta) > 0
            and (out_w, out_h) == (full_w, full_h)
            and abs(box.cx - full.cx) < 0.5
            and abs(box.cr - full.cr) < 0.5
        ):
            ctx.progress(0.9, f"crop: {what} covers the whole frame - passing through")
            return _write(out_image, data, header)

        ctx.progress(0.5, f"crop: {what} {full_w}x{full_h} -> {out_w}x{out_h}")
        planes, layout = _to_planes(data)
        cropped = _from_planes(cut(planes, box), layout)
        _write(out_image, cropped, cut_header(header, box, out_w, out_h))
        ctx.progress(1.0, "crop: done")
        return _result(out_image)


def _to_planes(arr: np.ndarray) -> tuple[np.ndarray, str]:
    if arr.ndim == 2:
        return arr[None], "2d"
    if arr.ndim == 3 and arr.shape[0] in (3, 4):
        return arr, "chw"
    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        return np.moveaxis(arr, -1, 0), "hwc"
    raise RuntimeError(f"crop: unsupported FITS shape {arr.shape}")


def _from_planes(planes: np.ndarray, layout: str) -> np.ndarray:
    if layout == "2d":
        return planes[0]
    if layout == "hwc":
        return np.moveaxis(planes, 0, -1)
    return planes


def _write(out_image: Path, data: np.ndarray, header: fits.Header) -> dict[str, Ref]:
    fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
    return _result(out_image)


# ---------------------------------------------------------------------------
# Helpers shared with (formerly) auto_crop — kept here so crop.py is
# self-contained after the merger.
# ---------------------------------------------------------------------------


def _spatial_shape(arr: np.ndarray) -> tuple[int, int]:
    """Return (height, width) of the spatial axes regardless of channel layout."""
    if arr.ndim == 2:
        return int(arr.shape[0]), int(arr.shape[1])
    if arr.ndim == 3 and arr.shape[0] in (3, 4):
        return int(arr.shape[1]), int(arr.shape[2])
    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        return int(arr.shape[0]), int(arr.shape[1])
    raise RuntimeError(f"crop: unsupported FITS shape {arr.shape}")


def _signal_mask(arr: np.ndarray, threshold: float) -> np.ndarray:
    """2D mask of pixels with any channel > threshold.

    Per-axis any() reduces RGB cubes to a 2D mask where a pixel counts as
    "real" if any channel is above threshold — preserves edge content where
    only one channel happens to be lit (rare in practice but cheap to handle).
    """
    above = np.isfinite(arr) & (arr > threshold)
    if arr.ndim == 2:
        return above
    if arr.ndim == 3 and arr.shape[0] in (3, 4):
        return above.any(axis=0)
    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        return above.any(axis=-1)
    raise RuntimeError(f"crop: unsupported FITS shape {arr.shape}")


EDGE_NODATA = 0.005
"""An edge row or column may be this much nodata (dead pixels, black star cores)."""


def _shrink_to_data(
    mask: np.ndarray, x: int, y: int, w: int, h: int, tol: float = EDGE_NODATA
) -> tuple[int, int, int, int]:
    """Move the emptiest edge of the box inward until every edge is real data.

    Greedy, one edge at a time, so a wedge on one side costs only that side.
    Never shrinks below a quarter of the box in either direction; a frame
    that empty is passed on as is rather than cropped to a sliver.
    """
    x0, x1, y0, y1 = x, x + w - 1, y, y + h - 1
    min_w, min_h = max(1, w // 4), max(1, h // 4)
    while x1 - x0 + 1 > min_w and y1 - y0 + 1 > min_h:
        gaps = {
            "top": 1 - mask[y0, x0 : x1 + 1].mean(),
            "bottom": 1 - mask[y1, x0 : x1 + 1].mean(),
            "left": 1 - mask[y0 : y1 + 1, x0].mean(),
            "right": 1 - mask[y0 : y1 + 1, x1].mean(),
        }
        side = max(gaps, key=gaps.get)
        if gaps[side] <= tol:
            return x0, y0, x1 - x0 + 1, y1 - y0 + 1
        if side == "top":
            y0 += 1
        elif side == "bottom":
            y1 -= 1
        elif side == "left":
            x0 += 1
        else:
            x1 -= 1
    return x, y, w, h


def _bbox_of_signal(arr: np.ndarray, threshold: float) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) of pixels with any channel > threshold."""
    mask = _signal_mask(arr, threshold)

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
