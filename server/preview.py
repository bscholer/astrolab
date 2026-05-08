"""On-demand thumbnail previews for cache artifacts.

The UI shows a tiny stretched preview next to each completed node so the user
can eyeball whether calibration ate the gradient, whether registration was
clean, etc. We render once and cache the PNG inside the same cache entry so
subsequent loads are essentially free.

Strategy:
- image/png        pass-through (the file IS already a viewable PNG)
- image/fits       autostretch (asinh) -> PNG
- master/fits      same as image/fits
- sequence/fits    pick a representative frame, treat as image/fits
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from astropy.io import fits
from PIL import Image

from .cache import DONE_MARKER, ContentCache

log = logging.getLogger("astrolab.preview")

THUMB_LONG_EDGE = 512
"""Thumbnail size in pixels (long edge). Big enough to evaluate stretch
quality, small enough to fit four side by side without wrapping."""

PREVIEW_FILENAME = "_preview.png"


class PreviewError(RuntimeError):
    pass


def render_preview(cache: ContentCache, node_hash: str, port: str) -> Path:
    """Return the path to a cached PNG preview for `(node_hash, port)`.

    Renders on first call and caches inside the entry dir as `_preview_<port>.png`.
    Raises PreviewError if the cache entry is missing or the artifact isn't
    something we know how to render.
    """
    entry = cache.lookup(node_hash)
    if entry is None:
        raise PreviewError(f"no committed cache entry for {node_hash}")

    target = _locate_artifact(entry, port)
    if target is None:
        raise PreviewError(f"port '{port}' not found in {entry}")

    out = entry / f"_preview_{port}.png"
    # Only re-render if missing or stale relative to the source artifact.
    if out.exists() and out.stat().st_mtime >= target.stat().st_mtime:
        return out

    suffix = target.suffix.lower()
    if target.is_file() and suffix == ".png":
        # Pass-through: just resize to thumbnail. We could symlink but a real
        # copy keeps the cache entry self-contained.
        img = Image.open(target).convert("RGB")
        img.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))
        img.save(out, "PNG", optimize=True)
        return out

    if target.is_file() and suffix in (".fit", ".fits"):
        _render_fits_to_png(target, out)
        return out

    if target.is_dir():
        # sequence/fits: pick a representative frame. Prefer .fit/.fits files,
        # ignore .seq index. Use the median-named file (sorted) so previews
        # are deterministic across re-runs.
        fits_frames = sorted(
            p for p in target.iterdir()
            if p.is_file() and p.suffix.lower() in (".fit", ".fits")
            and not p.name.startswith("_")
        )
        if not fits_frames:
            raise PreviewError(f"no FITS frames under {target}")
        rep = fits_frames[len(fits_frames) // 2]
        _render_fits_to_png(rep, out)
        return out

    raise PreviewError(f"don't know how to preview {target}")


def _locate_artifact(entry: Path, port: str) -> Path | None:
    """Find the file or dir matching this port inside the cache entry.

    Cache entries store outputs at one of two shapes:
      - <entry>/<port>.<ext>  (file)
      - <entry>/<port>/...    (directory)
    """
    dir_match = entry / port
    if dir_match.is_dir() and dir_match.name != DONE_MARKER:
        return dir_match
    matches = sorted(p for p in entry.glob(f"{port}.*") if p.name != DONE_MARKER)
    return matches[0] if matches else None


def _render_fits_to_png(src: Path, dst: Path) -> None:
    """Read a FITS file, autostretch, downscale, write a PNG."""
    with fits.open(src, memmap=False) as hdul:
        data = hdul[0].data
        if data is None:
            for hdu in hdul[1:]:
                if hdu.data is not None:
                    data = hdu.data
                    break
    if data is None:
        raise PreviewError(f"FITS at {src} has no image data")

    arr = np.asarray(data)
    # Common Siril output is 1-channel 32-bit float. Some captures are
    # 3-channel cubes (R, G, B as separate planes). Handle both.
    if arr.ndim == 2:
        rgb = _stretch_mono(arr)
    elif arr.ndim == 3 and arr.shape[0] in (3, 4):
        rgb = np.stack([_stretch_mono(arr[i]) for i in range(3)], axis=-1)
    elif arr.ndim == 3 and arr.shape[-1] in (3, 4):
        rgb = np.stack([_stretch_mono(arr[..., i]) for i in range(3)], axis=-1)
    else:
        # Unexpected shape; fall back to flattening to mono.
        rgb = _stretch_mono(arr.reshape(-1, arr.shape[-1]).mean(axis=0))

    if rgb.ndim == 2:
        img = Image.fromarray(rgb, mode="L").convert("RGB")
    else:
        img = Image.fromarray(rgb, mode="RGB")

    img.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))
    img.save(dst, "PNG", optimize=True)


def _stretch_mono(plane: np.ndarray) -> np.ndarray:
    """Asinh stretch a single channel -> uint8 array.

    Robust min/max via percentiles so a few hot pixels don't blow out the
    stretch. Asinh softens highlights (like Siril's default ScreenStretch).
    """
    arr = plane.astype(np.float32)
    finite = np.isfinite(arr)
    if not finite.all():
        arr = np.where(finite, arr, np.nan)
        lo, hi = np.nanpercentile(arr, [0.5, 99.5])
        arr = np.where(np.isnan(arr), lo, arr)
    else:
        lo, hi = np.percentile(arr, [0.5, 99.5])

    span = max(hi - lo, 1e-9)
    norm = np.clip((arr - lo) / span, 0.0, 1.0)
    # Asinh stretch with knee at 0.1 brightens the midtones without crushing
    # bright stars. Naztronomy-style.
    stretched = np.arcsinh(norm * 10.0) / np.arcsinh(10.0)
    return (stretched * 255.0).astype(np.uint8)
