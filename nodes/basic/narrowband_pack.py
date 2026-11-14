"""narrowband_pack: put a night's H-alpha and OIII stacks into one HOO image.

Input ports : ha   (IMAGE_FITS) - aligned H-alpha stack from narrowband_extract
              oiii (IMAGE_FITS) - aligned OIII stack from narrowband_extract
Output port : image (IMAGE_FITS) - RGB, red H-alpha, green and blue OIII, linear

Duo-band data on a colour camera has to be split into H-alpha (red pixels)
and OIII (green and blue pixels) before debayering: the demosaic fills each
pixel's missing colours from its neighbours using the other channels'
detail, so after debayering green and blue carry H-alpha's structure. On
the California Nebula the debayered green+blue came out at 1.02 x red with
almost nothing else in it; split first, OIII and H-alpha correlate at 0.3.

Packing the two stacks into one RGB image per night lets the rest of the
stack-per-night chain stay as it is: combine_stacks joins nights, and the
finishing steps (channel_map scales OIII against H-alpha) see an ordinary
colour image. No levels are changed here.

Pure numpy.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from astropy.io import fits
from pydantic import BaseModel

from nodes._seq_runner import image_ref
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class NarrowbandPackParams(BaseModel):
    pass


def _read(path: Path) -> tuple[np.ndarray, fits.Header]:
    with fits.open(path, memmap=False) as hdul:
        data, header = hdul[0].data, hdul[0].header.copy()
    if data is None:
        raise RuntimeError(f"narrowband_pack: no image data in {path}")
    data = np.asarray(data, dtype=np.float32)
    if data.ndim == 3:
        data = data.mean(axis=0)
    return data, header


def pack(ha: np.ndarray, oiii: np.ndarray) -> np.ndarray:
    """(H, W) x2 -> (3, H, W) HOO; trims to the common size if they differ by a pixel."""
    h, w = min(ha.shape[0], oiii.shape[0]), min(ha.shape[1], oiii.shape[1])
    ha, oiii = ha[:h, :w], oiii[:h, :w]
    return np.stack([ha, oiii, oiii]).astype(np.float32)


@register("narrowband_pack")
class NarrowbandPackNode(Node[NarrowbandPackParams]):
    id = "narrowband_pack"
    version = 1
    tier = "keep"

    inputs = {"ha": PortType.IMAGE_FITS, "oiii": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = NarrowbandPackParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: NarrowbandPackParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_image = Path(out_dir) / "image.fit"  # type: ignore[arg-type]
        ha, header = _read(inputs["ha"].path)
        oiii, _ = _read(inputs["oiii"].path)
        ctx.progress(0.5, "narrowband_pack: H-alpha to red, OIII to green and blue")
        fits.PrimaryHDU(pack(ha, oiii), header=header).writeto(out_image, overwrite=True)
        ctx.progress(1.0, "narrowband_pack: done")
        return {"image": image_ref(out_image)}
