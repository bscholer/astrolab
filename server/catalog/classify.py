"""Universal scope-and-image-type classifier.

The classifier is the single decision point for "is this a frame we care
about, and if so what kind". It takes an already-parsed FITS primary header
and a path, and returns either a :class:`Classified` describing how to file
the frame, or ``None`` for "skip this file".

Per-file classification means a single capture root can mix multiple
scopes - we don't pick one adapter up front and assume everything beneath
follows that convention.

The classifier never touches the filesystem. It's a pure function over
``(header_dict, Path)``, which keeps the surface small and the unit tests
header-only.

Scope detection
---------------

Each scope writes a distinctive marker into one well-known FITS keyword.
This survives firmware updates, user-organized folder trees, and
hand-renamed files - which path-based detection does not.

============  ===========  ============================================
scope_id      keyword      pattern (case-insensitive prefix match)
============  ===========  ============================================
``dwarf3``    TELESCOP     ``DWARFIII``
``dwarf3``    ORIGIN       ``DWARFLAB``
``seestar``   CREATOR      ``ZWO Seestar``
``seestar``   INSTRUME     ``Seestar``
``asiair``    CREATOR      ``ZWO ASIAIR``
``nina``      SWCREATE     ``N.I.N.A.``
============  ===========  ============================================

The first matching rule wins. ``None`` is returned if no rule matches; the
scanner counts those as unrecognized and surfaces a count to the UI but
does not error.

Image-type detection
--------------------

Most scopes set ``IMAGETYP`` to one of ``LIGHT``/``DARK``/``FLAT``/``BIAS``
(case varies, plus aliases like ``LIGHT FRAME``). NINA / ASIAIR / Seestar
all do; Dwarf 3 does not - so for Dwarf 3 we use the path as a fallback:

- ``CALI_FRAME/`` → not a frame at all (those are factory masters, handled
  separately) - classifier returns ``None`` so the scanner skips them.
- ``DWARF_DARK/`` → ``DARK``.
- otherwise → ``LIGHT``.

Filename skips
--------------

Some firmware-emitted artifacts live alongside the raw subs and are not
themselves subs (e.g. Dwarf 3 deposits stacked previews under
``stacked-*.fits``). They are filtered here so the rest of the pipeline
never sees them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .models import ImageType, Quality


class Classified(BaseModel):
    """How the classifier wants this frame filed."""

    model_config = ConfigDict(extra="forbid")

    scope_id: str
    image_type: ImageType
    quality: Quality = "ok"


SKIP_FILENAME_PREFIXES: tuple[str, ...] = (
    "stacked-",  # Dwarf 3 firmware-stacked output left next to the subs
    "img_",      # Dwarf 3 preview thumbnails (also .png/.tif but defensive)
)
"""Prefixes that mark firmware-emitted artifacts living alongside subs.
These are not themselves raw frames and must not enter the frames table."""


SKIP_SUFFIXES: tuple[str, ...] = (
    ".png",
    ".tif",
    ".tiff",
    ".jpg",
    ".jpeg",
)
"""Non-FITS sidecars that may appear in a capture tree but never matter."""


_IMAGETYP_ALIASES: dict[str, ImageType] = {
    "light": "LIGHT",
    "light frame": "LIGHT",
    "lights": "LIGHT",
    "dark": "DARK",
    "dark frame": "DARK",
    "darks": "DARK",
    "flat": "FLAT",
    "flat frame": "FLAT",
    "flats": "FLAT",
    "bias": "BIAS",
    "bias frame": "BIAS",
    "biases": "BIAS",
    "offset": "BIAS",
}


def _hstr(header: dict[str, Any], key: str) -> str:
    """Return ``header[key]`` as a stripped string, or ``""`` if missing."""
    value = header.get(key)
    if value is None:
        return ""
    return str(value).strip()


def detect_scope(header: dict[str, Any]) -> str | None:
    """Map a FITS primary header to a scope_id, or ``None`` if unknown.

    First match wins. Header keys we look at are documented at module top.
    """
    telescop = _hstr(header, "TELESCOP")
    origin = _hstr(header, "ORIGIN")
    if telescop.upper().startswith("DWARFIII") or origin.upper().startswith("DWARFLAB"):
        return "dwarf3"

    creator = _hstr(header, "CREATOR")
    if creator.lower().startswith("zwo seestar"):
        return "seestar"
    instrume = _hstr(header, "INSTRUME")
    if instrume.lower().startswith("seestar"):
        return "seestar"

    if creator.lower().startswith("zwo asiair"):
        return "asiair"

    swcreate = _hstr(header, "SWCREATE")
    if swcreate.lower().startswith("n.i.n.a."):
        return "nina"

    return None


def detect_image_type(
    header: dict[str, Any], path: Path, scope_id: str
) -> ImageType | None:
    """Return the image type, or ``None`` if this file isn't a frame at all.

    Dwarf 3 frames don't set ``IMAGETYP``; for them we fall back to a path
    rule that also lets us reject ``CALI_FRAME/`` files (handled separately
    by the factory-masters walker, not the frames pipeline).
    """
    raw = _hstr(header, "IMAGETYP").lower()
    if raw:
        mapped = _IMAGETYP_ALIASES.get(raw)
        if mapped is not None:
            return mapped
        # Unknown IMAGETYP value - treat as not-a-frame. Better to skip
        # than to mis-classify into the lights pile.
        return None

    if scope_id == "dwarf3":
        parts = {p.lower() for p in path.parts}
        if "cali_frame" in parts:
            return None  # factory master - separate walker handles these
        if "dwarf_dark" in parts:
            return "DARK"
        return "LIGHT"

    return None


def _detect_quality(path: Path) -> Quality:
    """Dwarf 3 flags rejected subs by prefixing the filename with ``failed_``."""
    return "failed" if path.name.startswith("failed_") else "ok"


def _is_derived_frame(header: dict[str, Any]) -> bool:
    """Return True if the header marks this file as a Siril-derived product
    rather than a raw sub.

    Two independent signals, OR'd together:

    - ``STACKCNT >= 2`` — FITS-standard "number of frames combined". A raw
      single exposure can't have this; any value >= 2 means the file is a
      master (stack of lights, master dark/flat/bias, etc.).
    - ``PROGRAM`` starting with ``Siril`` — Siril stamps this on every FITS
      it writes, including the per-frame outputs of ``seqextract_HaOIII``
      which don't carry STACKCNT but are still not raw subs (each one is
      one channel extracted from a Bayer sub).

    Either is sufficient: a Siril stack hits both, a Siril extraction hits
    only PROGRAM. Derived files like these belong in the masters table (or
    not in the catalog at all); routing them through the frames pipeline
    spawns ghost sessions with derivative FILTER strings like ``Astro_Ha``
    or ``mixed`` that the matcher can't do anything useful with.
    """
    stackcnt = header.get("STACKCNT")
    if stackcnt is not None:
        try:
            if int(stackcnt) >= 2:
                return True
        except (TypeError, ValueError):
            pass
    return _hstr(header, "PROGRAM").lower().startswith("siril")


def classify(header: dict[str, Any], path: Path) -> Classified | None:
    """Return the classifier's decision for one FITS file, or ``None`` to skip.

    The header is whatever ``read_primary_header`` produced; missing keys
    are absent, not ``None``. ``path`` is used for filename skips, the
    quality flag, and Dwarf 3's image-type fallback only - we do not stat
    or read the file.
    """
    name = path.name
    suffix = path.suffix.lower()
    if suffix in SKIP_SUFFIXES:
        return None
    for prefix in SKIP_FILENAME_PREFIXES:
        if name.startswith(prefix):
            return None

    if _is_derived_frame(header):
        return None

    scope_id = detect_scope(header)
    if scope_id is None:
        return None

    image_type = detect_image_type(header, path, scope_id)
    if image_type is None:
        return None

    return Classified(
        scope_id=scope_id,
        image_type=image_type,
        quality=_detect_quality(path),
    )
