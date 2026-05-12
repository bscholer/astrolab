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


# Cross-scope header builders. Values reflect what each capture program
# actually writes (verified against starbash's per-scope header dumps under
# https://github.com/geeksville/starbash/tree/main/doc/fits as of May 2026).
# We don't reproduce starbash's code or files; these are clean-room
# header dictionaries that exercise our classifier and scanner across
# scopes without raw FITS files we don't have rights to.


def asiair_light_header(**overrides: Any) -> dict[str, Any]:
    """ZWO ASIAIR-flavored light frame header.

    The discriminator is ``CREATOR``; everything else is just realistic
    accompanying metadata so the scanner can populate session rows.
    """
    base = {
        "CREATOR": "ZWO ASIAIR Plus",
        "IMAGETYP": "Light",
        "OBJECT": "M 31",
        "DATE-OBS": "2025-08-25T05:52:13.488",
        "EXPTIME": 30.0,
        "EXPOSURE": 30.0,
        "GAIN": 100,
        "FILTER": "None",
        "INSTRUME": "ZWO ASI2600MC Duo",
        "TELESCOP": "OnStep",
        "XBINNING": 1,
        "YBINNING": 1,
        "XPIXSZ": 3.76,
        "YPIXSZ": 3.76,
        "FOCALLEN": 494,
        "CCD-TEMP": -10.0,
        "RA": 10.99584,
        "DEC": 41.415833,
        "BAYERPAT": "RGGB",
    }
    base.update(overrides)
    return base


def nina_light_header(**overrides: Any) -> dict[str, Any]:
    """N.I.N.A.-flavored light frame header.

    Discriminator is ``SWCREATE``; NINA writes both ``EXPOSURE`` and
    ``EXPTIME`` for the same value.
    """
    base = {
        "SWCREATE": "N.I.N.A. 3.2.0.3005 (x64)",
        "IMAGETYP": "LIGHT",
        "OBJECT": "M 27",
        "DATE-OBS": "2025-09-17T03:34:02.647",
        "EXPTIME": 120.0,
        "EXPOSURE": 120.0,
        "GAIN": 100,
        "FILTER": "HaOiii",
        "INSTRUME": "ZWO ASI2600MC Duo",
        "TELESCOP": "Ascar V 80mm extender",
        "XBINNING": 1,
        "YBINNING": 1,
        "XPIXSZ": 3.76,
        "YPIXSZ": 3.76,
        "FOCALLEN": 600.0,
        "CCD-TEMP": -10.0,
        "SET-TEMP": -10.0,
        "RA": 299.882295,
        "DEC": 22.718996,
        "BAYERPAT": "RGGB",
    }
    base.update(overrides)
    return base


def seestar_light_header(**overrides: Any) -> dict[str, Any]:
    """ZWO Seestar S30/S50-flavored light frame header.

    Discriminator is ``CREATOR`` (also ``INSTRUME``). Note ``BAYERPAT`` is
    ``GRBG``, not the ``RGGB`` everyone else writes.
    """
    base = {
        "CREATOR": "ZWO Seestar S50",
        "IMAGETYP": "Light",
        "OBJECT": "M 101",
        "DATE-OBS": "2025-07-05T06:23:58.931",
        "EXPTIME": 10.0,
        "EXPOSURE": 10.0,
        "GAIN": 80,
        "FILTER": "IRCUT",
        "INSTRUME": "Seestar S50",
        "TELESCOP": "S50_153dd4e2",
        "XBINNING": 1,
        "YBINNING": 1,
        "XPIXSZ": 2.9,
        "YPIXSZ": 2.9,
        "FOCALLEN": 250,
        "CCD-TEMP": 18.375,
        "RA": 211.000005,
        "DEC": 54.233611,
        "BAYERPAT": "GRBG",
    }
    base.update(overrides)
    return base


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
