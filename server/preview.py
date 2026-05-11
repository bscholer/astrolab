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
import tempfile
from pathlib import Path

import numpy as np
from astropy.io import fits
from PIL import Image

from .cache import DONE_MARKER, ContentCache
from .siril import SirilNotFound, SirilRuntime, find_siril

log = logging.getLogger("astrolab.preview")

THUMB_LONG_EDGE = 512
"""Thumbnail size in pixels (long edge). Big enough to evaluate stretch
quality, small enough to fit four side by side without wrapping."""

PREVIEW_FILENAME = "_preview.png"


class PreviewError(RuntimeError):
    pass


def render_preview(
    cache: ContentCache,
    node_hash: str,
    port: str,
    *,
    neutral: bool = True,
) -> Path:
    """Return the path to a cached PNG preview for `(node_hash, port)`.

    Renders on first call and caches inside the entry dir as `_preview_<port>.png`
    (or `_preview_<port>_raw.png` when neutral=False, so both lineages can
    coexist).

    `neutral=True` (default) makes OSC stages stop looking like swampy green
    rectangles. Pre-stack pipeline outputs (calibrate → register) carry the
    Bayer 2x-green imbalance straight through; with neutral=True we stretch
    each channel independently so the rendered preview balances on its own.
    The actual cache data is untouched. Pass `neutral=False` to bypass the
    rebalance — useful for debugging when you want to see what Siril sees.

    Raises PreviewError if the cache entry is missing or the artifact isn't
    something we know how to render.
    """
    entry = cache.lookup(node_hash)
    if entry is None:
        raise PreviewError(f"no committed cache entry for {node_hash}")

    # Prefer the per-entry manifest so previews work for multi-output nodes
    # whose filenames don't follow the <port>.<ext> convention (eg
    # narrowband_extract writes `r_results_ha.fit` for port `ha`).
    manifest = cache.load_outputs(node_hash)
    target: Path | None
    if manifest is not None and port in manifest:
        target = manifest[port].path
        if not target.exists():
            target = None
    else:
        target = _locate_artifact(entry, port)
    if target is None:
        raise PreviewError(f"port '{port}' not found in {entry}")

    # Neutral and raw lineages live side by side so flipping the toggle
    # doesn't trigger a re-render of the other.
    suffix_tag = "" if neutral else "_raw"
    out = entry / f"_preview_{port}{suffix_tag}.png"
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
        _render_fits_to_png(target, out, neutral=neutral)
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
        _render_fits_to_png(rep, out, neutral=neutral)
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


def _render_fits_to_png(src: Path, dst: Path, *, neutral: bool = True) -> None:
    """Read a FITS file, autostretch, downscale, write a PNG.

    Strategy: try Siril's autostretch first (gold standard, matches what the
    user sees opening the FITS in Siril directly). Fall back to a numpy MTF
    stretch when Siril isn't available — Mac dev / CI / etc.

    `neutral=True` switches Siril from `autostretch -linked` (one curve
    across all channels, preserves color relationships including the OSC
    pre-rgb_equal green dominance) to plain `autostretch` (per-channel,
    each channel lands at the same target background). The numpy fallback
    already stretches per-channel so it's neutral by construction; we
    only branch the Siril path.

    OSC raws (Dwarf 3 lights pre-debayer) carry BAYERPAT='RGGB' as a 2D
    plane; in the numpy fallback we half-res debayer first because mono
    rendering of a Bayer mosaic looks like sparkly noise.
    """
    if _render_via_siril(src, dst, neutral=neutral):
        return
    _render_fits_to_png_numpy(src, dst)


def _render_via_siril(src: Path, dst: Path, *, neutral: bool = True) -> bool:
    """Render `src` via Siril's autostretch and resize into `dst`. Returns
    False (no exception) when Siril isn't available or the run fails, so
    the caller can fall back to the numpy path.

    Siril's `autostretch` assumes the working buffer is in [0, 1]. Most of
    our intermediate FITS aren't:
      * convert outputs are uint16 raw Bayer (effective range ~[60, 4095])
      * calibrate / register outputs are float32 centered near zero with
        small negative tails (post-debayer, post-dark-sub)
    Feeding those directly produces degenerate MTF params (Siril logs
    `Applying MTF with values 0.000000, 0.000000, 1.000000`) and the
    preview comes out all-black or all-white. We pre-normalize each input
    to [0, 1] via robust percentile clipping into a temp FITS, then let
    Siril autostretch that.
    """
    try:
        binary = find_siril()
    except SirilNotFound:
        return False

    runtime = SirilRuntime(binary=binary)
    with tempfile.TemporaryDirectory(prefix="astrolab-preview-") as td:
        td_path = Path(td)
        normalized = td_path / "input.fit"
        if not _write_normalized_fits(src, normalized):
            return False
        out_stem = td_path / "preview"
        # Plain `autostretch` is per-channel; `-linked` shares one curve
        # across all channels. Pre-rgb_equal OSC data has G ~2x R/B, so
        # linked carries that imbalance into the preview. Per-channel
        # neutralizes it with no effect on the cached image data.
        stretch_cmd = "autostretch" if neutral else "autostretch -linked"
        commands = [
            f"load {_siril_quote(normalized)}",
            stretch_cmd,
            f"savepng {_siril_quote(out_stem)}",
        ]
        try:
            result = runtime.run(commands, working_dir=td_path, timeout=60.0)
        except Exception:
            log.exception("siril preview crashed for %s; falling back to numpy", src)
            return False
        if result.returncode != 0:
            log.warning(
                "siril preview returned %d for %s; falling back to numpy.\n%s",
                result.returncode,
                src,
                result.stdout[-1500:],
            )
            return False
        out_png = out_stem.with_suffix(".png")
        if not out_png.exists():
            log.warning("siril preview ran but produced no PNG at %s", out_png)
            return False
        # Siril savepng writes 16-bit PNG at full resolution. PIL opens 16-bit
        # mono PNGs as mode 'I' (32-bit int with 0..65535 values); a naive
        # .convert('RGB') clips everything >255 to white instead of scaling.
        # Scale down to 8-bit ourselves before letting PIL touch RGB.
        img = Image.open(out_png)
        if img.mode in ("I", "I;16"):
            arr = np.asarray(img, dtype=np.uint32)
            img = Image.fromarray(np.clip(arr // 256, 0, 255).astype(np.uint8), mode="L")
        img = img.convert("RGB")
        img.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))
        img.save(dst, "PNG", optimize=True)
    return True


def _write_normalized_fits(src: Path, dst: Path) -> bool:
    """Read `src`, robust-normalize the data to [0, 1], write to `dst`.

    Single-pass percentile clip [0.5, 99.95] across the whole array (not
    per-channel) so RGB cubes keep their channel relationships, then Siril
    autostretch acts on a sane range. Returns False if the source has no
    image data; callers fall back to the numpy renderer.
    """
    try:
        with fits.open(src, memmap=False) as hdul:
            data = hdul[0].data
            header = hdul[0].header
            if data is None:
                for hdu in hdul[1:]:
                    if hdu.data is not None:
                        data = hdu.data
                        header = hdu.header
                        break
        if data is None:
            return False
        arr = np.asarray(data).astype(np.float32, copy=False)
        finite_mask = np.isfinite(arr)
        if not finite_mask.any():
            return False
        finite = arr[finite_mask]
        lo = float(np.percentile(finite, 0.5))
        hi = float(np.percentile(finite, 99.95))
        span = max(hi - lo, 1e-9)
        norm = np.clip((arr - lo) / span, 0.0, 1.0).astype(np.float32)
        norm = np.where(np.isfinite(norm), norm, 0.0).astype(np.float32)
        # Drop BSCALE/BZERO so the normalized values aren't reinterpreted on
        # read; everything else (BAYERPAT, NAXIS3 for cubes) we keep.
        clean = header.copy()
        for key in ("BSCALE", "BZERO", "DATAMIN", "DATAMAX"):
            if key in clean:
                del clean[key]
        fits.PrimaryHDU(data=norm, header=clean).writeto(dst, overwrite=True)
        return True
    except Exception:
        log.exception("preview: normalize-FITS failed for %s", src)
        return False


def _siril_quote(path: Path) -> str:
    """Quote a path for inclusion in a Siril SSF command."""
    s = str(path)
    if any(c in s for c in (" ", "\t", '"')):
        return '"' + s.replace('"', r"\"") + '"'
    return s


def _render_fits_to_png_numpy(src: Path, dst: Path) -> None:
    """Numpy fallback path: percentile + MTF autostretch, half-res debayer
    when BAYERPAT is set. Used when Siril isn't on $PATH (Mac dev, CI)."""
    with fits.open(src, memmap=False) as hdul:
        data = hdul[0].data
        header = hdul[0].header
        if data is None:
            for hdu in hdul[1:]:
                if hdu.data is not None:
                    data = hdu.data
                    header = hdu.header
                    break
    if data is None:
        raise PreviewError(f"FITS at {src} has no image data")

    arr = np.asarray(data)

    # Common Siril output is 1-channel 32-bit float, possibly Bayer-patterned.
    # Some captures are 3-channel cubes (R, G, B as separate planes).
    if arr.ndim == 2:
        bayerpat = str(header.get("BAYERPAT") or "").strip().upper()
        rgb = (
            _debayer_half_res(arr, bayerpat)
            if bayerpat in _BAYER_OFFSETS
            else _stretch_mono(arr)
        )
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


# Each entry maps a Bayer pattern to (r_offset, g1_offset, g2_offset, b_offset)
# where each offset is (row, col) inside the 2x2 super-pixel. Example RGGB:
#   R G       R = (0,0)  G1 = (0,1)
#   G B       G2 = (1,0)  B = (1,1)
_BayerOffsets = tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]
_BAYER_OFFSETS: dict[str, _BayerOffsets] = {
    "RGGB": ((0, 0), (0, 1), (1, 0), (1, 1)),
    "BGGR": ((1, 1), (0, 1), (1, 0), (0, 0)),
    "GRBG": ((0, 1), (0, 0), (1, 1), (1, 0)),
    "GBRG": ((1, 0), (0, 0), (1, 1), (0, 1)),
}


def _debayer_half_res(plane: np.ndarray, pattern: str) -> np.ndarray:
    """Half-resolution debayer of a 2D Bayer plane to a stretched RGB uint8.

    Each 2x2 super-pixel collapses to one RGB sample (R from R, B from B,
    G as the average of the two G sites). Half-res is fine for a 512px
    thumbnail and avoids pulling in cv2 / colour-demosaicing as deps.
    """
    h, w = plane.shape
    # Crop to even dims so the 2:: slicing is clean.
    if h % 2:
        plane = plane[:-1]
    if w % 2:
        plane = plane[:, :-1]

    (r_off, g1_off, g2_off, b_off) = _BAYER_OFFSETS[pattern]
    r = plane[r_off[0]::2, r_off[1]::2].astype(np.float32)
    g1 = plane[g1_off[0]::2, g1_off[1]::2].astype(np.float32)
    g2 = plane[g2_off[0]::2, g2_off[1]::2].astype(np.float32)
    b = plane[b_off[0]::2, b_off[1]::2].astype(np.float32)
    g = (g1 + g2) * 0.5

    return np.stack(
        [_stretch_mono(r), _stretch_mono(g), _stretch_mono(b)],
        axis=-1,
    )


def _stretch_mono(plane: np.ndarray) -> np.ndarray:
    """Autostretch a single channel via median-MAD + midtones transfer function.

    Mirrors Siril's autostretch / PixInsight's STF: pick the shadow point
    from `median - 2.8*MAD` (so background noise lands near zero), then
    solve for the midtone parameter that maps the median to `BG_TARGET`
    (~0.25, a tasteful dark gray). The MTF curve preserves star pinpoints
    where the old percentile+asinh was washing the stack into pure white.
    """
    arr = plane.astype(np.float32)
    finite = np.isfinite(arr)
    if not finite.all():
        arr = np.where(finite, arr, 0.0)

    # Pre-normalize to [0, 1] using a tiny shadow nudge to absorb dead pixels
    # without throwing away real signal. amax = true max so bright stars
    # can saturate at 1.0 (we want them white).
    amin = float(np.percentile(arr, 0.01))
    amax = float(arr.max())
    span = max(amax - amin, 1e-9)
    norm = np.clip((arr - amin) / span, 0.0, 1.0)

    median = float(np.median(norm))
    mad = float(np.median(np.abs(norm - median))) or 1e-6

    # Shadow clip: a few MADs below the median, never below zero. After this
    # rescale the median sits at `nm` in [shadow, 1].
    shadow = max(median - 2.8 * mad, 0.0)
    nm = max((median - shadow) / max(1.0 - shadow, 1e-9), 1e-6)

    # Solve for the midtone `m` such that MTF(nm, m) == BG_TARGET. Standard
    # closed form; clamp `m` to a sane range so a degenerate input (constant
    # plane, etc.) doesn't blow up the curve.
    bg_target = 0.25
    denom = nm * (2.0 * bg_target - 1.0) - bg_target
    m_param = (nm * (bg_target - 1.0)) / denom if abs(denom) > 1e-9 else 0.5
    m_param = float(np.clip(m_param, 0.01, 0.99))

    x = np.clip((norm - shadow) / max(1.0 - shadow, 1e-9), 0.0, 1.0)
    # MTF: f(x) = (m-1)x / ((2m-1)x - m). Vectorized; avoid divide-by-zero
    # by leaning on the clamp on m_param plus a small epsilon on the denom.
    d = (2.0 * m_param - 1.0) * x - m_param
    d = np.where(np.abs(d) < 1e-9, -m_param, d)
    out = (m_param - 1.0) * x / d
    out = np.clip(out, 0.0, 1.0)
    return (out * 255.0).astype(np.uint8)
