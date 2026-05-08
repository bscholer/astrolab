"""Helpers for building tiny synthetic FITS files used in catalog tests.

Real Dwarf 3 frames are 16 MB each; synthetic ones here are tens of KB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

DEFAULT_LIGHT_HEADER: dict[str, Any] = {
    "OBJECT": "M 33",
    "DATE-OBS": "2025-10-21T22:19:29.504",
    "EXPTIME": 30.0,
    "GAIN": 60,
    "FILTER": "Astro",
    "CAMERA": "TELE",
    "INSTRUME": "DWARFIII",
    "TELESCOP": "DWARFIII",
    "ORIGIN": "DWARFLAB",
    "XBINNING": 1,
    "YBINNING": 1,
    "XPIXSZ": 2.0,
    "YPIXSZ": 2.0,
    "FOCALLEN": 150.0,
    "DET-TEMP": 24,
    "RA": 23.4682,
    "DEC": 30.65859,
    "BAYERPAT": "RGGB",
}


DEFAULT_DARK_HEADER: dict[str, Any] = {
    "DATE-OBS": "2025-10-21T00:38:14.624",
    "EXPTIME": 30.0,
    "GAIN": 60,
    "CAMERA": "TELE",
    "INSTRUME": "DWARFIII",
    "TELESCOP": "DWARFIII",
    "XBINNING": 1,
    "YBINNING": 1,
    "DET-TEMP": 22,
}


def write_fits(
    path: Path,
    *,
    headers: dict[str, Any] | None = None,
    shape: tuple[int, int] = (16, 16),
) -> None:
    """Write a minimal FITS file with the given header values at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.zeros(shape, dtype=np.uint16)
    hdu = fits.PrimaryHDU(data=data)
    if headers:
        for k, v in headers.items():
            hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
