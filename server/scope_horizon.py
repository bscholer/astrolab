"""Find the sky in a wide-camera frame. Smooth, bright or blue areas that connect to sure sky."""

from __future__ import annotations

from collections import deque

import numpy as np
from PIL import Image, ImageFilter

SIZE = (320, 180)
SEED_MIN_PIXELS = 1500
TEXTURE_SEED = 5.0
TEXTURE_GROW = 14.0
BLUE_MARGIN = 8.0
BRIGHT_SKY = 175.0


def _box_mean(a: np.ndarray, k: int) -> np.ndarray:
    pad = k // 2
    p = np.pad(a, pad, mode="edge")
    c = p.cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    h, w = a.shape
    s = c[k : k + h, k : k + w] - c[0:h, k : k + w] - c[k : k + h, 0:w] + c[0:h, 0:w]
    return s / (k * k)


def _components(mask: np.ndarray) -> list[np.ndarray]:
    """4-connected components as lists of flat pixel indices."""
    h, w = mask.shape
    seen = np.zeros(mask.size, dtype=bool)
    flat = mask.ravel()
    found = []
    for start in np.flatnonzero(flat):
        if seen[start]:
            continue
        seen[start] = True
        queue, members = deque([start]), []
        while queue:
            i = queue.popleft()
            members.append(i)
            y, x = divmod(i, w)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w:
                    j = ny * w + nx
                    if flat[j] and not seen[j]:
                        seen[j] = True
                        queue.append(j)
        found.append(np.array(members))
    return found


def _smooth_edges(mask: np.ndarray, radius: int) -> np.ndarray:
    img = Image.fromarray((mask * 255).astype(np.uint8))
    size = 2 * radius + 1
    closed = img.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))
    return np.asarray(closed) > 127


def sky_mask(image: Image.Image) -> np.ndarray:
    """True where a pixel is sky. The array has shape (180, 320)."""
    a = np.asarray(image.convert("RGB").resize(SIZE)).astype(float)
    gray = a.mean(2)
    mean = _box_mean(gray, 7)
    texture = np.sqrt(np.maximum(_box_mean(gray * gray, 7) - mean**2, 0))
    blue = a[..., 2] - a[..., 0]
    seed = (texture < TEXTURE_SEED) & (gray > 105) & ((blue > BLUE_MARGIN) | (gray > BRIGHT_SKY))
    seed_ok = np.zeros(seed.shape, dtype=bool)
    for members in _components(seed):
        if len(members) < SEED_MIN_PIXELS:
            continue
        if blue.ravel()[members].mean() > BLUE_MARGIN or gray.ravel()[members].mean() > BRIGHT_SKY:
            seed_ok.ravel()[members] = True
    grow = (gray > 100) & (texture < TEXTURE_GROW) & (blue > 0)
    candidate = _smooth_edges(grow | seed_ok, 2)
    out = np.zeros(seed.shape, dtype=bool)
    for members in _components(candidate):
        if seed_ok.ravel()[members].any():
            out.ravel()[members] = True
    return _smooth_edges(out, 3)


_mask_cache: dict[tuple[str, float], np.ndarray] = {}


def cached_mask(path) -> np.ndarray:
    key = (str(path), path.stat().st_mtime)
    if key not in _mask_cache:
        _mask_cache[key] = sky_mask(Image.open(path))
    return _mask_cache[key]
