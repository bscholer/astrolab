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
colour image. No levels are changed here; OIII is moved by the sub-pixel
offset measured against H-alpha (see pack()).

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


MAX_SHIFT_PX = 4.0
"""Larger measured offsets are distrusted (a bad correlation, not Bayer geometry)."""


def _stars(x: np.ndarray) -> np.ndarray:
    """Stars only: everything 5 sigma above the sky, the rest zero."""
    finite = x[np.isfinite(x)]
    sky = float(np.median(finite))
    sigma = 1.4826 * float(np.median(np.abs(finite - sky)))
    return np.clip(np.nan_to_num(x) - sky - 5 * sigma, 0, None)


def measure_shift(ref: np.ndarray, img: np.ndarray) -> tuple[float, float]:
    """(dy, dx) to move `img` onto `ref`, to a fraction of a pixel.

    Cross-correlation of the stars in a central crop. Its peak is about
    Gaussian (stars are), so a parabola through the logs of the peak and its
    neighbours lands on the fraction; whitened phase correlation has a
    needle peak that biases that fit toward whole pixels.
    """
    h, w = ref.shape
    cy, cx = h // 2, w // 2
    r = min(h, w) // 3
    a = _stars(ref[cy - r : cy + r, cx - r : cx + r])
    b = _stars(img[cy - r : cy + r, cx - r : cx + r])
    c = np.fft.ifft2(np.fft.fft2(a) * np.conj(np.fft.fft2(b))).real
    py, px = np.unravel_index(int(c.argmax()), c.shape)

    def refine(m1: float, m0: float, p1: float) -> float:
        if min(m1, m0, p1) <= 0:
            return 0.0
        m1, m0, p1 = np.log(m1), np.log(m0), np.log(p1)
        d = m1 - 2 * m0 + p1
        return float(0.5 * (m1 - p1) / d) if d != 0 else 0.0

    n, m = c.shape
    dy = py + refine(c[(py - 1) % n, px], c[py, px], c[(py + 1) % n, px])
    dx = px + refine(c[py, (px - 1) % m], c[py, px], c[py, (px + 1) % m])
    dy = dy - n if dy > n / 2 else dy
    dx = dx - m if dx > m / 2 else dx
    return float(dy), float(dx)


def shift(img: np.ndarray, dy: float, dx: float) -> np.ndarray:
    """Sub-pixel translate by a Fourier phase ramp; the edge that wraps is zeroed."""
    h, w = img.shape
    ky = np.fft.fftfreq(h)[:, None]
    kx = np.fft.fftfreq(w)[None, :]
    out = np.fft.ifft2(np.fft.fft2(img) * np.exp(-2j * np.pi * (ky * dy + kx * dx))).real
    iy, ix = int(np.ceil(abs(dy))), int(np.ceil(abs(dx)))
    if iy:
        if dy > 0:
            out[:iy] = 0
        else:
            out[-iy:] = 0
    if ix:
        if dx > 0:
            out[:, :ix] = 0
        else:
            out[:, -ix:] = 0
    return out.astype(np.float32)


def pack(
    ha: np.ndarray, oiii: np.ndarray, align: bool = True
) -> tuple[np.ndarray, tuple[float, float]]:
    """(H, W) x2 -> (3, H, W) HOO, OIII moved onto H-alpha; returns the shift used.

    Trims to the common size if they differ by a pixel. The two stacks share
    one registration, but H-alpha comes from the red pixels upscaled, which
    sit half a pixel from the green and blue ones, so a small offset is
    measured and removed.
    """
    h, w = min(ha.shape[0], oiii.shape[0]), min(ha.shape[1], oiii.shape[1])
    ha, oiii = ha[:h, :w], oiii[:h, :w]
    used = (0.0, 0.0)
    if align:
        dy, dx = measure_shift(ha, oiii)
        if max(abs(dy), abs(dx)) <= MAX_SHIFT_PX and (abs(dy) > 0.05 or abs(dx) > 0.05):
            oiii = shift(oiii, dy, dx)
            used = (dy, dx)
    return np.stack([ha, oiii, oiii]).astype(np.float32), used


@register("narrowband_pack")
class NarrowbandPackNode(Node[NarrowbandPackParams]):
    id = "narrowband_pack"
    version = 2
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
        rgb, (dy, dx) = pack(ha, oiii)
        ctx.progress(0.7, f"narrowband_pack: OIII moved {dx:+.2f}, {dy:+.2f} px onto H-alpha")
        fits.PrimaryHDU(rgb, header=header).writeto(out_image, overwrite=True)
        ctx.progress(1.0, "narrowband_pack: done")
        return {"image": image_ref(out_image)}
