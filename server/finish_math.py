"""Finishing steps as plain numpy, on (3, H, W) or (H, W) float32 arrays.

Two users:
- the quick preview (server/quick_preview.py) runs the whole finishing chain
  on a downscaled copy of the image in a fraction of a second, so the editor's
  sliders can update the picture while they move;
- starnet_recombine runs the last touches (tone, sharpen, tighten stars) on
  the full image, so the preview and the final render share that code.

stretch, color_balance and auto_bp_shift still run through Siril in the
pipeline. The versions here follow Siril's maths (autostretch, `mtf`,
`asinh`, SCNR average neutral) closely enough for a preview;
tests/test_finish_math.py pins their behaviour.
"""

from __future__ import annotations

import numpy as np

MAD_NORM = 1.4826


def _channels(data: np.ndarray) -> list[np.ndarray]:
    return [data[c] for c in range(data.shape[0])] if data.ndim == 3 else [data]


def mtf(x: np.ndarray | float, m: float) -> np.ndarray | float:
    """Midtones transfer function: 0 -> 0, m -> 0.5, 1 -> 1."""
    return (m - 1.0) * x / ((2.0 * m - 1.0) * x - m)


def apply_mtf(data: np.ndarray, black, mid, high=1.0) -> np.ndarray:
    """Siril's `mtf`: rescale [black, high] to [0, 1], then the midtones curve.

    black and mid may be scalars or one value per channel.
    """
    out = np.empty_like(data, dtype=np.float32)
    chans = _channels(data)
    blacks = np.broadcast_to(np.asarray(black, np.float32), (len(chans),))
    mids = np.broadcast_to(np.asarray(mid, np.float32), (len(chans),))
    highs = np.broadcast_to(np.asarray(high, np.float32), (len(chans),))
    for i, ch in enumerate(chans):
        span = max(float(highs[i] - blacks[i]), 1e-9)
        x = np.clip((ch - blacks[i]) / span, 0.0, 1.0)
        res = mtf(x, float(mids[i]))
        if out.ndim == 3:
            out[i] = res
        else:
            out[:] = res
    return out


def _stats(ch: np.ndarray) -> tuple[float, float]:
    """Median and normalised MAD of the lit pixels of a channel, on a sample."""
    flat = ch.ravel()
    if flat.size > 2_000_000:
        flat = flat[:: flat.size // 2_000_000]
    lit = flat[np.isfinite(flat) & (flat != 0)]
    if lit.size == 0:
        return 0.0, 0.0
    med = float(np.median(lit))
    return med, float(MAD_NORM * np.median(np.abs(lit - med)))


def autostretch_params(
    data: np.ndarray, shadows_clip: float = -2.8, target_bg: float = 0.25, linked: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """(black, midtones) per channel, as Siril's `autostretch` picks them.

    The shadows sit `shadows_clip` sigma (normalised MAD) from the median and
    the midtones balance puts the median at `target_bg`. Linked averages the
    channels' shadows and medians first, so one curve serves all three.
    """
    stats = [_stats(ch) for ch in _channels(data)]
    if linked:
        c0 = max(0.0, float(np.mean([m + shadows_clip * s for m, s in stats])))
        med = float(np.mean([m for m, _ in stats]))
        mid = float(mtf(max(med - c0, 1e-9), target_bg))
        n = len(stats)
        return np.full(n, c0, np.float32), np.full(n, mid, np.float32)
    blacks, mids = [], []
    for m, s in stats:
        c0 = max(0.0, m + shadows_clip * s)
        blacks.append(c0)
        mids.append(float(mtf(max(m - c0, 1e-9), target_bg)))
    return np.array(blacks, np.float32), np.array(mids, np.float32)


def asinh_stretch(
    data: np.ndarray, beta: float, offset: float = 0.0, human: bool = False
) -> np.ndarray:
    """Siril's `asinh`: a colour-preserving arcsinh curve on luminance."""
    x = np.clip(data - offset, 0.0, None).astype(np.float32)
    if x.ndim == 3 and x.shape[0] == 3:
        w = np.array([0.2126, 0.7152, 0.0722] if human else [1 / 3, 1 / 3, 1 / 3], np.float32)
        lum = np.tensordot(w, x, axes=1)
    else:
        lum = x
    k = np.arcsinh(beta * lum) / (np.maximum(lum, 1e-9) * np.arcsinh(beta))
    return np.clip(x * k, 0.0, 1.0).astype(np.float32)


def saturate(data: np.ndarray, amount: float) -> np.ndarray:
    """Scale each pixel's colour away from its own luminance (stretch's saturation)."""
    if data.ndim != 3 or data.shape[0] != 3 or amount == 1.0:
        return data
    lum = data.mean(axis=0, keepdims=True)
    return np.clip(lum + amount * (data - lum), 0.0, 1.0).astype(np.float32)


def scnr(data: np.ndarray, amount: float = 1.0) -> np.ndarray:
    """SCNR green removal, average neutral: green no higher than (red + blue) / 2.

    Siril keeps each pixel's lightness; scaling the pixel back to its old mean
    does the same for a preview. `amount` blends between off and full.
    """
    if data.ndim != 3 or data.shape[0] != 3 or amount <= 0:
        return data
    r, g, b = data[0], data[1], data[2]
    g2 = np.minimum(g, 0.5 * (r + b))
    g2 = g + amount * (g2 - g)
    out = np.stack([r, g2, b]).astype(np.float32)
    before = data.mean(axis=0)
    after = out.mean(axis=0)
    scale = np.where(after > 1e-9, before / np.maximum(after, 1e-9), 1.0)
    return np.clip(out * scale[None], 0.0, None).astype(np.float32)


def _sky(channel: np.ndarray, lit: np.ndarray, block: int = 8, pct: float = 10) -> float:
    h, w = (channel.shape[0] // block) * block, (channel.shape[1] // block) * block
    if h == 0 or w == 0:
        return 0.0
    c = channel[:h, :w].reshape(h // block, block, w // block, block).swapaxes(1, 2)
    m = lit[:h, :w].reshape(h // block, block, w // block, block).swapaxes(1, 2)
    full = m.reshape(h // block, w // block, -1).all(axis=2)
    meds = np.median(c.reshape(h // block, w // block, -1), axis=2)[full]
    return float(np.percentile(meds, pct)) if meds.size else 0.0


def bp_shift(data: np.ndarray, clip_fraction: float, neutral_sky: bool = True) -> np.ndarray:
    """auto_bp_shift: the black point that clips `clip_fraction` of the pixels.

    With neutral_sky each colour channel moves by how far its own sky sits
    from the mean, as the node does. Zero (no data) stays zero.
    """
    data = data.astype(np.float32)
    if data.ndim == 3 and data.shape[0] == 3:
        lit = (data != 0).any(axis=0)
        if not lit.any():
            return data
        sample = data[:, lit][:, ::7]
        bp = float(np.quantile(sample, clip_fraction))
        if neutral_sky:
            sky = np.array([_sky(data[c], lit) for c in range(3)])
            bps = np.clip(bp + (sky - sky.mean()), 0.0, 0.999999)
        else:
            bps = np.full(3, min(max(bp, 0.0), 0.999999))
        out = np.empty_like(data)
        for c in range(3):
            out[c] = np.clip((data[c] - bps[c]) / (1.0 - bps[c]), 0.0, 1.0)
        out[:, ~lit] = 0.0
        return out
    bp = min(max(float(np.quantile(data, clip_fraction)), 0.0), 0.999999)
    return np.clip((data - bp) / (1.0 - bp), 0.0, 1.0).astype(np.float32)


# ---- last touches, on stretched data in [0, 1] -------------------------------


def _box(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    """Mean over a window of 2r+1 along one axis, edges clamped."""
    if r <= 0:
        return a
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
    n = a.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian-like blur of a 2-D array: three box passes per axis."""
    if sigma <= 0.3:
        return a
    r = max(1, int(round(sigma * 0.85)))
    out = a.astype(np.float32)
    for _ in range(3):
        out = _box(_box(out, r, 0), r, 1)
    return out


def _luma(data: np.ndarray) -> np.ndarray:
    return data.mean(axis=0) if data.ndim == 3 else data


def _with_luma(data: np.ndarray, old: np.ndarray, new: np.ndarray) -> np.ndarray:
    """Give each pixel the new luminance and keep its colour."""
    if data.ndim != 3:
        return np.clip(new, 0.0, 1.0).astype(np.float32)
    ratio = np.where(old > 1e-6, new / np.maximum(old, 1e-6), 1.0)
    out = data * ratio[None]
    flat = old <= 1e-6
    if flat.any():
        out[:, flat] = new[flat][None]
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def tone(data: np.ndarray, contrast: float = 0.0, highlights: float = 0.0,
         shadows: float = 0.0) -> np.ndarray:
    """Contrast, highlights and shadows, each in [-1, 1], on luminance.

    Each is a smooth curve that leaves black and white where they are:
    contrast an S around the middle grey, shadows a lift or dip peaking at a
    third of the range, highlights the same at two thirds.
    """
    if not (contrast or highlights or shadows):
        return data
    lum = np.clip(_luma(data), 0.0, 1.0)
    x = lum
    y = x + 2.0 * contrast * x * (1 - x) * (2 * x - 1)
    y = y + 1.5 * shadows * x * (1 - x) ** 2
    y = y + 1.5 * highlights * x**2 * (1 - x)
    return _with_luma(data, lum, np.clip(y, 0.0, 1.0))


def sharpen(data: np.ndarray, amount: float, sigma: float = 1.5) -> np.ndarray:
    """Unsharp mask on luminance. amount 0-1; sigma in pixels of this image."""
    if amount <= 0:
        return data
    lum = _luma(data)
    detail = lum - blur(lum, sigma)
    return _with_luma(data, lum, np.clip(lum + 1.5 * amount * detail, 0.0, 1.0))


def erode(a: np.ndarray) -> np.ndarray:
    """3x3 minimum filter of a 2-D array."""
    p = np.pad(a, 1, mode="edge")
    out = a.copy()
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            np.minimum(out, p[dy : dy + a.shape[0], dx : dx + a.shape[1]], out=out)
    return out


def tighten_stars(stars: np.ndarray, amount: float, steps: float = 1.0) -> np.ndarray:
    """Pull a stars layer's edges in. amount 0-1; steps scales the erosion to
    the image (1 at full size, less on a downscaled preview)."""
    if amount <= 0:
        return stars
    weight = float(np.clip(amount * steps, 0.0, 1.0))
    eroded = np.stack([erode(c) for c in _channels(stars)]) if stars.ndim == 3 else erode(stars)
    if stars.ndim == 3:
        eroded = eroded.reshape(stars.shape)
    return (stars + weight * (eroded - stars)).astype(np.float32)


def shrink_stars(stars: np.ndarray, amount: float) -> np.ndarray:
    """Fade a stretched stars layer: identity at 1, nothing at 0 (faint wings first)."""
    if amount >= 1.0:
        return stars
    if amount <= 0.0:
        return np.zeros_like(stars)
    return np.power(np.clip(stars, 0.0, 1.0), 1.0 / amount).astype(stars.dtype, copy=False)


def split_stars(data: np.ndarray, size: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """A quick (starless, stars) split for the preview, where StarNet is too slow.

    A grey opening (erode then dilate) of radius ~2.5 px times `size` removes
    the stars and keeps extended light; what it removed is the stars layer.
    """
    r = max(1, int(round(2.5 * size)))
    base = np.stack(_channels(data)) if data.ndim == 3 else data[None]
    starless = np.empty_like(base)
    for i, ch in enumerate(base):
        e = ch
        for _ in range(r):
            e = erode(e)
        d = e
        for _ in range(r):
            d = -erode(-d)
        starless[i] = np.minimum(d, ch)
    stars = np.clip(base - starless, 0.0, None)
    if data.ndim != 3:
        return starless[0], stars[0]
    return starless, stars
