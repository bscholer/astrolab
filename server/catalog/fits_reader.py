"""FITS primary-header reader.

Reads only the primary HDU's header; we never touch pixel data here. astropy
pre-reads the first 2880-byte block lazily, so this is cheap (~ms per file).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from astropy.io import fits

_PANEL_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")
"""Trailing panel suffix used by Dwarf 3 mosaic mode in the OBJECT header,
e.g. 'M 31(1)' for panel 1 of an M 31 mosaic. We strip this so all panels
of one mosaic land under the same target row."""

HEADER_KEYS_TO_KEEP: tuple[str, ...] = (
    "OBJECT",
    "DATE-OBS",
    "EXPTIME",
    "EXPOSURE",
    "GAIN",
    "FILTER",
    "CAMERA",
    "INSTRUME",
    "TELESCOP",
    "ORIGIN",
    "CREATOR",
    "SWCREATE",
    "XBINNING",
    "YBINNING",
    "XPIXSZ",
    "YPIXSZ",
    "FOCALLEN",
    "DET-TEMP",
    "CCD-TEMP",
    "SET-TEMP",
    "RA",
    "DEC",
    "BAYERPAT",
    "IMAGETYP",
    "NAXIS1",
    "NAXIS2",
    "STACKCNT",
    "PROGRAM",
)
"""Headers we care about. Anything else is dropped to keep blob size sane."""


def read_primary_header(path: Path) -> dict[str, Any]:
    """Return a dict of selected headers from `path`'s primary HDU.

    Missing keys are simply absent from the returned dict; callers should
    coalesce with adapter hints when a key isn't present.
    """
    out: dict[str, Any] = {}
    with fits.open(path, memmap=False, do_not_scale_image_data=True) as hdul:
        header = hdul[0].header
        for key in HEADER_KEYS_TO_KEEP:
            if key in header:
                value = header[key]
                if isinstance(value, str):
                    value = value.strip()
                out[key] = value
    return out


def normalize_target(name: str) -> str:
    """Collapse whitespace, strip trailing mosaic '(N)' panel suffix."""
    collapsed = " ".join(name.split())
    return _PANEL_SUFFIX_RE.sub("", collapsed).strip()
