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


CELL_DEG = 2.0
AZ_BINS = int(360 / CELL_DEG)
CENTER_FRACTION = 0.2
OVERCAST_MIN_GRAY = 140.0
OVERCAST_MAX_COLOR = 12.0
_mask_cache: dict[tuple[str, float], np.ndarray] = {}


def cached_mask(path) -> np.ndarray:
    key = (str(path), path.stat().st_mtime)
    if key not in _mask_cache:
        _mask_cache[key] = sky_mask(Image.open(path))
    return _mask_cache[key]


def center_is_sky(image: Image.Image, mask: np.ndarray) -> bool:
    """Whether the middle of a frame is mostly sky. Only the middle is placed reliably.

    Flat gray overcast fails the mask, which wants blue or glare, so a bright, colorless,
    smooth middle counts as sky too.
    """
    h, w = mask.shape
    dh, dw = int(h * CENTER_FRACTION / 2), int(w * CENTER_FRACTION / 2)
    if mask[h // 2 - dh : h // 2 + dh, w // 2 - dw : w // 2 + dw].mean() > 0.5:
        return True
    a = np.asarray(image.convert("RGB").resize((w, h))).astype(float)
    a = a[h // 2 - dh : h // 2 + dh, w // 2 - dw : w // 2 + dw]
    gray = a.mean(2)
    color = (a.max(2) - a.min(2)).mean()
    texture = np.abs(np.diff(gray, axis=1)).mean()
    return bool(gray.mean() > OVERCAST_MIN_GRAY and color < OVERCAST_MAX_COLOR and texture < 5)


def tile_centers(folder, model, excluded: set[str]) -> list[tuple[float, float, bool]]:
    """(alt, az, sky) where the middle of each scan frame points, as hints for drawing."""
    from server.scope_pano import tile_grid, tile_name

    pitches, rotations = tile_grid(folder)
    out = []
    for pitch in pitches:
        for rot in rotations:
            path = folder / tile_name(pitch, rot)
            if tile_name(pitch, rot) in excluded or not path.is_file():
                continue
            alt, az = model.to_sky(pitch, rot)
            sky = center_is_sky(Image.open(path), cached_mask(path))
            out.append((float(alt), float(az), sky))
    return out


def profile_from_points(points: list[tuple[float, float]]) -> list[float]:
    """Horizon altitude every CELL_DEG of azimuth, joining drawn (az, alt) points around."""
    pts = sorted((az % 360, alt) for az, alt in points)
    az = np.array([a for a, _ in pts])
    alt = np.array([v for _, v in pts])
    az = np.concatenate([az - 360, az, az + 360])
    alt = np.concatenate([alt, alt, alt])
    centers = np.arange(AZ_BINS) * CELL_DEG + CELL_DEG / 2
    return [round(float(v), 1) for v in np.interp(centers, az, alt)]


def horizon_at(profile: list[float | None], az_deg: float) -> float | None:
    """The horizon altitude at an azimuth, None where nothing is known."""
    if not profile:
        return None
    return profile[int((az_deg % 360) / CELL_DEG) % len(profile)]


def horizon_svg(
    profile: list[float | None],
    hints: list[tuple[float, float, bool]],
    drawn: list[tuple[float, float]],
    floor_deg: float,
) -> str:
    """A sky map of the horizon line: zenith at the center, the true horizon at the rim.

    North is up and east is right, like a map of the yard. The shaded band is blocked.
    hints are (alt, az, sky) for the middle of each scan frame. drawn are the (az, alt)
    points the line was drawn through. The UI maps taps back with the same geometry:
    a 360 square, the rim 26 in from the edge.
    """
    size, pad = 360, 26
    c, rim = size / 2, size / 2 - pad

    def xy(alt: float, az: float) -> tuple[float, float]:
        r = rim * (90 - max(min(alt, 90.0), 0.0)) / 90
        return c + r * np.sin(np.radians(az)), c - r * np.cos(np.radians(az))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        'font-family="system-ui, sans-serif" font-size="11">',
        "<title>Horizon seen from the scope</title>",
    ]
    ring = (
        '<circle cx="{c}" cy="{c}" r="{r:.1f}" fill="none" stroke="#6f8099" stroke-width="{w}"{d}/>'
    )
    for alt in (0, 30, 60):
        width = 1 if alt == 0 else 0.5
        parts.append(ring.format(c=c, r=rim * (90 - alt) / 90, w=width, d=""))
        if alt:
            x, y = xy(alt, 0)
            parts.append(f'<text x="{x + 3:.1f}" y="{y - 3:.1f}" fill="#6f8099">{alt}°</text>')
    if floor_deg not in (0, 30, 60):
        dash = ' stroke-dasharray="3 3"'
        parts.append(ring.format(c=c, r=rim * (90 - floor_deg) / 90, w=1, d=dash))
    for label, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        x, y = xy(-8, az)
        parts.append(
            f'<text x="{x:.1f}" y="{y + 4:.1f}" fill="#e9f1ff" text-anchor="middle">{label}</text>'
        )
    seen = [(i * CELL_DEG + CELL_DEG / 2, v) for i, v in enumerate(profile or []) if v is not None]
    if seen:
        line = [xy(v, az) for az, v in seen]
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in line)
        closed = len(seen) == len(profile)
        if closed:
            # Blocked band: the rim, then back around along the horizon line.
            outer = [xy(0, az) for az, _ in seen]
            band = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in outer) + " Z "
            band += "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in reversed(line)) + " Z"
            parts.append(
                f'<path d="{band}" fill="#6f8099" fill-opacity="0.35" fill-rule="evenodd"/>'
            )
            path += " Z"
        parts.append(f'<path d="{path}" fill="none" stroke="#5eead4" stroke-width="2"/>')
    for alt, az, is_sky in hints:
        if alt < 0:
            continue
        x, y = xy(alt, az)
        fill, what = ("#86b6ef", "sky") if is_sky else ("#eb6834", "blocked")
        parts.append(
            f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" rx="2" fill="{fill}">'
            f"<title>scan frame at altitude {alt:.0f}°, azimuth {az:.0f}°: {what}</title></rect>"
        )
    for az, alt in drawn:
        x, y = xy(alt, az)
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#e9f1ff">'
            f"<title>azimuth {az:.0f}°, clear above {alt:.0f}°</title></circle>"
        )
    parts.append("</svg>")
    return "\n".join(parts)
