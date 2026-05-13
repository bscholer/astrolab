"""crop: rectangular crop, with auto-trim fallback when no user box is set.

Input port  : image (IMAGE_FITS) - typically the output of seq_stack or
              narrowband_compose (placed right after the stack/compose step,
              before stretch).
Output port : image (IMAGE_FITS) - same FITS, sliced to the requested rect.

Two operating modes
-------------------
Auto-trim (default, no user box)
    When width == 0 (the sentinel for "no user override yet"), the node
    computes the bounding box of pixels above `threshold` and crops to that
    box, optionally padded by `padding` pixels. This is the same
    threshold+padding algorithm that the old auto_crop node used: the
    pedestal_offset step pushes real signal ~0.01 above zero, so a 0.001
    cutoff cleanly separates the registration zero-fill border from real data.

User crop (explicit bbox)
    When the user drags a rectangle in the UI crop picker, the parent page
    calls onchange which PATCHes x/y/width/height (normalized to [0,1]) and
    sets enabled=True. On subsequent run() calls, the node uses those explicit
    coords instead of the auto-trim path.

Coordinates are stored in [0,1] (normalized to the input frame's spatial
dims) so the dragged rectangle in the UI stays valid even after upstream
nodes change the resolution (e.g. flipping resample mode draft <-> full).
The runtime maps normalized -> pixels at run time.

WCS keys CRPIX1/CRPIX2 (if present) are shifted so a future plate-solve on
the cropped frame stays consistent.

Pure numpy/astropy; no Siril round-trip.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field, model_validator

from nodes.base import Node
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
        "image height (0 = top, 1 = bottom). "
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
        # Only clamp when the user has explicitly set a box (width > 0).
        if self.width > 0:
            if self.x + self.width > 1.0 + 1e-6:
                object.__setattr__(self, "width", max(1e-3, 1.0 - self.x))
            if self.y + self.height > 1.0 + 1e-6:
                object.__setattr__(self, "height", max(1e-3, 1.0 - self.y))
        return self


@register("crop")
class CropNode(Node[CropParams]):
    id = "crop"
    version = 2
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

        # Decide which crop rectangle to use.
        user_box = params.width > 0  # sentinel: 0 means "auto-trim"

        if user_box:
            # User dragged an explicit box in the UI.
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
                ctx.progress(0.9, "crop: full-frame user box — passing through")
                fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
                ctx.progress(1.0, "crop: done")
                return _result(out_image)

            ctx.progress(0.8, f"crop: user box {full_w}x{full_h} -> {w}x{h}")
        else:
            # Auto-trim: compute bounding box of signal above threshold.
            ctx.progress(0.3, "crop: auto-detecting signal bbox")
            x, y, w, h = _bbox_of_signal(data, params.threshold)

            if w <= 0 or h <= 0:
                # Nothing above threshold — keep the original rather than emitting
                # a zero-area FITS that would crash downstream.
                ctx.progress(0.9, "crop: no signal detected, passing through")
                fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
                ctx.progress(1.0, "crop: wrote pass-through")
                return _result(out_image)

            if params.padding > 0:
                pad = params.padding
                x = max(0, x - pad)
                y = max(0, y - pad)
                w = min(full_w - x, w + 2 * pad)
                h = min(full_h - y, h + 2 * pad)

            if x == 0 and y == 0 and w == full_w and h == full_h:
                ctx.progress(0.9, f"crop: nothing to trim ({full_w}x{full_h})")
                fits.PrimaryHDU(data=data, header=header).writeto(out_image, overwrite=True)
                ctx.progress(1.0, "crop: wrote pass-through")
                return _result(out_image)

            ctx.progress(
                0.8,
                f"crop: auto-trim {full_w}x{full_h} -> {w}x{h} "
                f"(trimmed {full_w - w}x{full_h - h})",
            )

        cropped = _slice_spatial(data, x, y, w, h)

        # Shift WCS reference pixel so plate-solve metadata stays consistent.
        # 1-based FITS convention: CRPIX1/2 reference the (1,1)-origin pixel.
        for key, off in (("CRPIX1", x), ("CRPIX2", y)):
            if key in header:
                with contextlib.suppress(TypeError, ValueError):
                    header[key] = float(header[key]) - off

        fits.PrimaryHDU(data=cropped, header=header).writeto(out_image, overwrite=True)
        ctx.progress(1.0, "crop: done")
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


def _slice_spatial(arr: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    if arr.ndim == 2:
        return arr[y : y + h, x : x + w]
    if arr.ndim == 3 and arr.shape[0] in (3, 4):
        return arr[:, y : y + h, x : x + w]
    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        return arr[y : y + h, x : x + w, :]
    raise RuntimeError(f"crop: unsupported FITS shape {arr.shape}")


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
        raise RuntimeError(f"crop: unsupported FITS shape {arr.shape}")

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
