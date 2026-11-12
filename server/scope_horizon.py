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


# The wide camera's field of view, across and up the frame. The DWARF 3 wide lens is about 45
# degrees across, and the frame is 16:9.
WIDE_HFOV_DEG = 45.0
WIDE_VFOV_DEG = 2 * np.degrees(np.arctan(np.tan(np.radians(WIDE_HFOV_DEG / 2)) * 9 / 16))
CELL_DEG = 2.0
SAMPLE_STEP = 4
AZ_BINS = int(360 / CELL_DEG)
ALT_BINS = int(180 / CELL_DEG)


def pixel_directions(model, pitch: float, rotation: float, h: int, w: int) -> np.ndarray:
    """Sky (east, north, up) unit vector for each pixel of a wide frame, shape (h, w, 3)."""
    d, up = model.vectors(pitch, rotation)
    right = np.cross(d, up)
    tx = np.tan(np.radians(WIDE_HFOV_DEG / 2))
    ty = np.tan(np.radians(WIDE_VFOV_DEG / 2))
    u = (2 * (np.arange(w) + 0.5) / w - 1) * tx
    v = (1 - 2 * (np.arange(h) + 0.5) / h) * ty
    rays = d + u[None, :, None] * right + v[:, None, None] * up
    return rays / np.linalg.norm(rays, axis=-1, keepdims=True)


def _cells(alt: np.ndarray, az: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    row = np.clip(((alt + 90) / CELL_DEG).astype(int), 0, ALT_BINS - 1)
    col = (az / CELL_DEG).astype(int) % AZ_BINS
    return row, col


def vote_grid(tiles, model) -> tuple[np.ndarray, np.ndarray]:
    """Sky and blocked counts per (altitude, azimuth) cell from (pitch, rotation, mask) tiles."""
    from server.scope_mount import alt_az

    sky_votes = np.zeros((ALT_BINS, AZ_BINS))
    blocked = np.zeros((ALT_BINS, AZ_BINS))
    for pitch, rotation, mask in tiles:
        m = mask[::SAMPLE_STEP, ::SAMPLE_STEP]
        h, w = mask.shape
        rays = pixel_directions(model, pitch, rotation, h, w)[::SAMPLE_STEP, ::SAMPLE_STEP]
        alt, az = alt_az(rays)
        row, col = _cells(alt, az)
        np.add.at(sky_votes, (row[m], col[m]), 1)
        np.add.at(blocked, (row[~m], col[~m]), 1)
    return sky_votes, blocked


def horizon_profile(sky_votes: np.ndarray, blocked: np.ndarray) -> list[float | None]:
    """For each azimuth cell, the top of the highest blocked cell. None where nothing was seen.

    A cell is blocked when more of its samples are not sky than are. A branch that hangs over
    sky still counts, so the line errs on the side of leaving a target out.
    """
    seen = (sky_votes + blocked) > 0
    is_blocked = seen & (blocked > sky_votes)
    out: list[float | None] = []
    for col in range(AZ_BINS):
        rows = np.flatnonzero(is_blocked[:, col])
        if rows.size:
            out.append(float((rows.max() + 1) * CELL_DEG - 90))
        elif seen[:, col].any():
            out.append(float(np.flatnonzero(seen[:, col]).min() * CELL_DEG - 90))
        else:
            out.append(None)
    return out


GAP_FILL_BINS = 5
_mask_cache: dict[tuple[str, float], np.ndarray] = {}


def _cached_mask(path) -> np.ndarray:
    key = (str(path), path.stat().st_mtime)
    if key not in _mask_cache:
        _mask_cache[key] = sky_mask(Image.open(path))
    return _mask_cache[key]


def _fill_gaps(profile: list[float | None]) -> list[float | None]:
    """Fill short runs of unseen azimuths with the higher of the two seen sides."""
    n = len(profile)
    if all(v is None for v in profile):
        return profile
    out = list(profile)
    for i in range(n):
        if profile[i] is not None:
            continue
        left = next((k for k in range(1, n) if profile[(i - k) % n] is not None), None)
        right = next((k for k in range(1, n) if profile[(i + k) % n] is not None), None)
        if left is None or right is None or left + right - 1 > GAP_FILL_BINS:
            continue
        out[i] = max(profile[(i - left) % n], profile[(i + right) % n])
    return out


def build_horizon(folder, model, excluded: set[str]) -> list[float | None]:
    """Horizon altitude for each CELL_DEG of azimuth, from the scan tiles not left out."""
    from server.scope_pano import tile_grid, tile_name

    pitches, rotations = tile_grid(folder)
    tiles = []
    for pitch in pitches:
        for rot in rotations:
            name = tile_name(pitch, rot)
            path = folder / name
            if name in excluded or not path.is_file():
                continue
            tiles.append((pitch, rot, _cached_mask(path)))
    return _fill_gaps(horizon_profile(*vote_grid(tiles, model)))


def horizon_at(profile: list[float | None], az_deg: float) -> float | None:
    """The horizon altitude at an azimuth, None where the scan saw nothing."""
    if not profile:
        return None
    return profile[int((az_deg % 360) / CELL_DEG) % len(profile)]


def horizon_svg(
    profile: list[float | None], points: list[tuple[float, float, bool]], floor_deg: float
) -> str:
    """A sky map of the horizon line: zenith at the center, the true horizon at the rim.

    North is up and east is right, like a map of the yard. The shaded band is blocked.
    points are (alt, az, used) for the slews behind the fit.
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
        for az, v in seen[:: max(1, len(seen) // 36)]:
            x, y = xy(v, az)
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="transparent">'
                f"<title>azimuth {az:.0f}°, clear above {v:.0f}°</title></circle>"
            )
    for alt, az, used in points:
        x, y = xy(alt, az)
        fill = "#e9f1ff" if used else "none"
        state = "used" if used else "left out"
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{fill}" '
            'stroke="#e9f1ff" stroke-width="1.5">'
            f"<title>slew at altitude {alt:.0f}°, azimuth {az:.0f}°, {state}</title></circle>"
        )
    parts.append("</svg>")
    return "\n".join(parts)
