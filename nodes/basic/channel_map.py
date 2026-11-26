"""channel_map: false-color palettes for one-shot-color data through a duo-band filter.

Input port  : image (IMAGE_FITS) - a linear, background-extracted RGB stack
Output port : image (IMAGE_FITS) - the remapped RGB stack, still linear

Through a duo-band (H-alpha + OIII) filter, a color camera records
H-alpha in red and OIII in green and blue (stack_per_night_duo splits them
properly before debayering and packs them that way). 'hoo' renders them in
the classic look: H-alpha red, OIII blue-teal.

Mixing the channels pixel by pixel does not get there on this data. OIII
is fainter and noisier, stars are much brighter in it (a wider band, three
quarters of the pixels), and a linked stretch keeps its nebula dim next to
its stars: the Rosette came out red with cyan stars at any OIII scale.
What the data does show is where OIII is relatively strong: on the Rosette
the OIII/H-alpha ratio is high in the core and low in the outer ring.

So color and brightness are taken separately, still in linear space:
- brightness: H-alpha plus OIII (OIII scaled to H-alpha's nebula spread),
  which keeps every bit of structure either channel has;
- color: the OIII share of the nebula, measured on 8x8 block medians
  (stars erased) and smoothed, mapped from red (H-alpha) to blue (OIII).
  The share is spread over its own 10th-90th percentile range, so the
  color shows where OIII is stronger or weaker than usual for this target.
  `oiii_strength` shifts the balance (1 as measured, higher bluer).
  The two ends of that ramp are hues: `ha_hue` (default 24, gold) and
  `oiii_hue` (default 215, azure), the warm-and-blue look of modern
  narrowband pictures. Ha 4 and OIII 213 give the classic red and teal HOO;
  pure red at full saturation clipped the red channel and looked blown out.
  The sky and stars stay neutral: color fades in with nebula signal and
  out on anything much brighter than its surroundings.
The stretch and its saturation then work as on any color image.

Pure numpy; no Siril round trip.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field

from nodes._seq_runner import image_ref
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class ChannelMapParams(BaseModel):
    enabled: bool = Field(
        default=False,
        description="Off passes the stack through. On remaps the channels into "
        "the chosen palette.",
        json_schema_extra={"agent_hint": "Turn on for duo-band nebula data only."},
    )
    palette: Literal["hoo"] = Field(
        default="hoo",
        description="'hoo': H-alpha red, OIII blue-teal, colored by where OIII "
        "is relatively strong.",
        json_schema_extra={"ui_section": "advanced"},
    )
    oiii_strength: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="Red-blue balance. 1 colors the nebula by its measured OIII "
        "share; higher turns more of it blue, lower more of it red.",
        json_schema_extra={
            "hash_precision": 3,
            "agent_hint": "Raise for more blue OIII in the core; lower for a redder nebula.",
        },
    )
    ha_hue: float = Field(
        default=24.0,
        ge=0.0,
        le=360.0,
        description="Color of H-alpha-rich nebula, as a hue in degrees: 24 gold, "
        "10 orange-red, 4 the classic HOO red.",
        json_schema_extra={"hash_precision": 1},
    )
    oiii_hue: float = Field(
        default=215.0,
        ge=0.0,
        le=360.0,
        description="Color of OIII-rich nebula, as a hue in degrees: 215 azure, "
        "190 teal, 213 the classic HOO blue-teal.",
        json_schema_extra={"hash_precision": 1},
    )


BLOCK = 8
HUE_SATURATION = 0.85
"""How far each end color sits from white. The stretch's saturation then scales it."""


def hue_rgb(hue_deg: float, saturation: float = HUE_SATURATION) -> np.ndarray:
    """An RGB color of full value at this hue (HSV)."""
    h = (hue_deg % 360.0) / 60.0
    x = 1.0 - abs(h % 2.0 - 1.0)
    rgb = [(1, x, 0), (x, 1, 0), (0, 1, x), (0, x, 1), (x, 0, 1), (1, 0, x)][int(h) % 6]
    return np.array([1.0 - saturation * (1.0 - c) for c in rgb], np.float32)


def _blocks(c: np.ndarray, k: int = BLOCK) -> np.ndarray:
    """Median of each k x k block: stars erased, extended light kept."""
    h, w = (c.shape[0] // k) * k, (c.shape[1] // k) * k
    tiles = c[:h, :w].reshape(h // k, k, w // k, k).swapaxes(1, 2)
    return np.median(tiles.reshape(h // k, w // k, -1), axis=2)


def _upsample(s: np.ndarray, shape: tuple[int, int], k: int = BLOCK) -> np.ndarray:
    """Bilinear from block centres back to full size."""
    h, w = shape
    ys = np.clip((np.arange(h) + 0.5) / k - 0.5, 0, s.shape[0] - 1)
    xs = np.clip((np.arange(w) + 0.5) / k - 0.5, 0, s.shape[1] - 1)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, s.shape[0] - 1), np.minimum(x0 + 1, s.shape[1] - 1)
    fy, fx = (ys - y0)[:, None], (xs - x0)[None, :]
    top = s[y0][:, x0] * (1 - fx) + s[y0][:, x1] * fx
    bottom = s[y1][:, x0] * (1 - fx) + s[y1][:, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(np.float32)


def _smooth(s: np.ndarray, r: int = 2) -> np.ndarray:
    """Median over each (2r+1)^2 neighbourhood of blocks.

    A median, not a mean: a block a bright star dominates is an outlier
    among its neighbours and drops out, instead of spreading into a square
    patch of color around the star.
    """
    p = np.pad(s, r, mode="edge")
    stack = [
        p[r + dy : r + dy + s.shape[0], r + dx : r + dx + s.shape[1]]
        for dy in range(-r, r + 1)
        for dx in range(-r, r + 1)
    ]
    return np.median(np.stack(stack), axis=0).astype(np.float32)


def _grow(m: np.ndarray, r: int) -> np.ndarray:
    """Max filter (square, radius r): widen the star mask over the halos."""
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out = np.maximum(out, np.roll(np.roll(m, dy, 0), dx, 1))
    return out


def _spread(c: np.ndarray, k: int = BLOCK) -> tuple[float, float]:
    """(sky, nebula signal above sky) on block medians, stars erased.

    Stars are relatively bright in OIII, so raw percentiles would match the
    stars and turn the nebula's OIII down (x0.66 instead of x0.91 on the
    Rosette). Sky is a low percentile: a nebula filling half the frame lifts
    the median.
    """
    b = _blocks(np.nan_to_num(c), k)
    b = b[b > 0]
    if b.size == 0:
        return 0.0, 0.0
    sky = float(np.percentile(b, 10))
    return sky, float(np.percentile(b, 99.5)) - sky


def hoo(
    data: np.ndarray,
    oiii_strength: float,
    ha_hue: float = 24.0,
    oiii_hue: float = 215.0,
    pixel_scale: float = 1.0,
) -> np.ndarray:
    """(3, H, W) RGB with H-alpha in red and OIII in green/blue -> HOO, linear.

    `pixel_scale` is how many full-size pixels one pixel of `data` covers (the quick
    preview's downscaled copy); block and star-halo sizes shrink with it so
    they cover the same patch of sky.
    """
    k = max(2, round(BLOCK / pixel_scale))
    halo = max(1, round(3 / pixel_scale))
    ha = np.nan_to_num(data[0].astype(np.float32))
    oiii = np.nan_to_num(0.5 * (data[1].astype(np.float32) + data[2].astype(np.float32)))
    lit = (ha > 0) & (oiii > 0)
    hsky, hspread = _spread(ha, k)
    osky, ospread = _spread(oiii, k)
    scale = hspread / ospread if ospread > 0 else 1.0
    h_lin = np.clip(ha - hsky, 0, None)
    o_lin = np.clip(oiii - osky, 0, None) * scale
    lum = h_lin + o_lin

    hb = _smooth(np.clip(_blocks(ha, k) - hsky, 0, None))
    ob = _smooth(np.clip(_blocks(oiii, k) - osky, 0, None) * scale)
    signal = hb + ob
    share = ob / np.maximum(signal, 1e-12)
    # Statistics only from blocks wholly inside the image: a union of offset
    # nights or a mosaic leaves empty canvas, which is not sky.
    inside = _blocks(lit.astype(np.float32), k) > 0.99
    # Color only on nebula: none below the median block (sky), full from the
    # 90th percentile up.
    if inside.sum() >= 10:
        floor, ref = np.percentile(signal[inside], [50, 90])
    else:
        floor, ref = 0.0, 0.0
    if ref > floor:
        weight = np.clip((signal - floor) / (ref - floor), 0, 1) * inside
    else:
        weight = np.zeros_like(signal)
    nebula = weight > 0.5
    if nebula.sum() >= 10:
        lo, hi = np.percentile(share[nebula], [10, 90])
    else:
        lo, hi = 0.0, 1.0
    t = np.clip((share - lo) / max(hi - lo, 1e-6) + 0.3 * (oiii_strength - 1.0), 0, 1)

    shape = ha.shape
    tf = _upsample(t, shape, k)
    wf = _upsample(weight, shape, k)
    # Stars stay white: color fades out where a pixel is far above its block.
    local = _upsample(_blocks(lum, k), shape, k)
    star = _grow(np.clip((lum / np.maximum(local, 1e-9) - 1.3) / 0.7, 0, 1), halo)
    amount = wf * (1 - star)
    ha_rgb, oiii_rgb = hue_rgb(ha_hue), hue_rgb(oiii_hue)
    col = (1 - tf)[None] * ha_rgb[:, None, None] + tf[None] * oiii_rgb[:, None, None]
    col = col / col.mean(axis=0, keepdims=True)
    col = 1 + amount[None] * (col - 1)
    out = lum[None] * col + max(hsky, 0.0)
    # Zero stays zero so the registration border is still nodata downstream.
    return np.where(lit[None], np.clip(out, 0.0, 1.0), 0.0).astype(np.float32)


@register("channel_map")
class ChannelMapNode(Node[ChannelMapParams]):
    id = "channel_map"
    version = 7
    tier = "keep"

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = ChannelMapParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: ChannelMapParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_image = Path(out_dir) / "image.fit"  # type: ignore[arg-type]
        src = inputs["image"].path
        if not src.exists():
            raise RuntimeError(f"channel_map: input image does not exist: {src}")
        if not params.enabled:
            ctx.progress(0.5, "channel_map: off - passing through")
            shutil.copy(src, out_image)
            return {"image": image_ref(out_image)}
        with fits.open(src, memmap=False) as hdul:
            data, header = hdul[0].data, hdul[0].header.copy()
        if data is None or data.ndim != 3 or data.shape[0] != 3:
            ctx.progress(0.5, "channel_map: not an RGB stack - passing through")
            shutil.copy(src, out_image)
            return {"image": image_ref(out_image)}
        ctx.progress(
            0.3,
            f"channel_map: {params.palette}, OIII x{params.oiii_strength:g}, "
            f"hues {params.ha_hue:g}/{params.oiii_hue:g}",
        )
        out = hoo(data, params.oiii_strength, params.ha_hue, params.oiii_hue)
        fits.PrimaryHDU(out, header=header).writeto(out_image)
        ctx.progress(1.0, "channel_map: done")
        return {"image": image_ref(out_image)}
