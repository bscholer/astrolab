"""channel_map: false-colour palettes for one-shot-colour data through a duo-band filter.

Input port  : image (IMAGE_FITS) - a linear, background-extracted RGB stack
Output port : image (IMAGE_FITS) - the remapped RGB stack, still linear

Through a duo-band (H-alpha + OIII) filter, a colour camera records
H-alpha in red and OIII in green and blue. 'hoo' maps red to H-alpha and
green and blue to OIII, the classic teal-and-red look. OIII is usually much
fainter, so it is scaled to H-alpha's spread (99.5th percentile above the
median) times `oiii_strength`; boosting it to full strength also boosts
its noise, so short integrations look cleaner below 1.

This is the cheap path on the finished stack. The narrowband template
extracts H-alpha and OIII from the raw CFA instead, which is cleaner but
means stacking each channel separately.

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
        description="'hoo': red is H-alpha, green and blue are OIII.",
        json_schema_extra={"ui_section": "advanced"},
    )
    oiii_strength: float = Field(
        default=0.6,
        ge=0.0,
        le=2.0,
        description="OIII relative to H-alpha after matching their spreads. 1 is "
        "equal; lower keeps OIII noise down on short integrations.",
        json_schema_extra={
            "hash_precision": 3,
            "agent_hint": "Raise for more teal OIII; lower if green/blue looks noisy.",
        },
    )


def _spread(c: np.ndarray) -> tuple[float, float]:
    sample = c[::4, ::4]
    sample = sample[np.isfinite(sample) & (sample > 0)]
    if sample.size == 0:
        return 0.0, 0.0
    med = float(np.median(sample))
    return med, float(np.percentile(sample, 99.5)) - med


def hoo(data: np.ndarray, oiii_strength: float) -> np.ndarray:
    """(3, H, W) RGB -> HOO, with OIII matched to H-alpha's spread."""
    ha = data[0].astype(np.float32)
    oiii = 0.5 * (data[1].astype(np.float32) + data[2].astype(np.float32))
    hm, hs = _spread(ha)
    om, osp = _spread(oiii)
    scale = (hs / osp if osp > 0 else 1.0) * oiii_strength
    # Zero stays zero so the registration border is still nodata downstream.
    mapped = np.where(oiii > 0, (oiii - om) * scale + hm, 0.0).astype(np.float32)
    return np.clip(np.stack([ha, mapped, mapped]), 0.0, 1.0)


@register("channel_map")
class ChannelMapNode(Node[ChannelMapParams]):
    id = "channel_map"
    version = 1
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
        ctx.progress(0.3, f"channel_map: {params.palette}, OIII x{params.oiii_strength:g}")
        fits.PrimaryHDU(hoo(data, params.oiii_strength), header=header).writeto(out_image)
        ctx.progress(1.0, "channel_map: done")
        return {"image": image_ref(out_image)}
