"""Draw what the scope's cameras would see, as JPEG: sky, yard, clouds and stars."""

from __future__ import annotations

import hashlib
import io
import math
from datetime import datetime

import numpy as np
from PIL import Image, ImageFilter

from server.fakescope.scene import Scene

FOV_DEG = {"wide": 45.0, "tele": 2.95}
OUT = (1280, 720)
WORK = (320, 180)


def _rng(*parts: object) -> np.random.Generator:
    return np.random.default_rng(
        int.from_bytes(hashlib.sha256(repr(parts).encode()).digest()[:4], "big")
    )


def pixel_alt_az(
    alt: float, az: float, fov: float, w: int, h: int
) -> tuple[np.ndarray, np.ndarray]:
    """Altitude and azimuth of each pixel for a level camera pointed at alt/az."""
    a, z = math.radians(alt), math.radians(az)
    d = np.array([math.cos(a) * math.sin(z), math.cos(a) * math.cos(z), math.sin(a)])
    right = np.array([math.cos(z), -math.sin(z), 0.0])
    up = np.cross(right, d)
    tx = math.tan(math.radians(fov / 2))
    ty = tx * h / w
    u = (2 * (np.arange(w) + 0.5) / w - 1) * tx
    v = (1 - 2 * (np.arange(h) + 0.5) / h) * ty
    rays = d + u[None, :, None] * right + v[:, None, None] * up
    rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
    return np.degrees(np.arcsin(np.clip(rays[..., 2], -1, 1))), np.degrees(
        np.arctan2(rays[..., 0], rays[..., 1])
    ) % 360


def _sky_color(sun_alt: float, alt: np.ndarray) -> np.ndarray:
    day = np.array([110, 160, 225.0])
    dusk = np.array([60, 60, 110.0])
    night = np.array([6, 8, 18.0])
    if sun_alt > 0:
        base = day
    elif sun_alt > -12:
        f = -sun_alt / 12
        base = (
            day * (1 - f) + dusk * f
            if sun_alt > -6
            else dusk * (1 - (f - 0.5) * 2) + night * (f - 0.5) * 2
        )
    else:
        base = night
    fade = np.clip(alt / 90, 0, 1)[..., None]
    return base * (0.8 + 0.2 * fade)


def render_frame(
    scene: Scene, camera: str, alt: float, az: float, at: datetime, sun: tuple[float, float]
) -> bytes:
    w, h = WORK
    fov = FOV_DEG[camera]
    palt, paz = pixel_alt_az(alt, az, fov, w, h)
    img = _sky_color(sun[0], palt)
    dark = sun[0] < -12
    if dark:
        stars = _rng(
            camera, round(alt, 1) if camera == "wide" else round(az, 2), round(az, 1)
        ).random((h, w))
        level = 0.995 if camera == "wide" else 0.985
        img[stars > level] = [235, 235, 235]
    skyline = np.vectorize(scene.skyline)(paz)
    ground = palt < 0
    blocked = (palt < skyline) & ~ground
    kinds = np.vectorize(lambda a, z: o.kind if (o := scene.obstacle_at(a, z)) else "fence")(
        palt, paz
    )
    colors = {
        "fence": ([45, 32, 24], [96, 66, 44]),
        "house": ([30, 28, 30], [150, 140, 130]),
        "tree": ([12, 20, 12], [40, 70, 35]),
    }
    for kind, (night_c, day_c) in colors.items():
        img[blocked & (kinds == kind)] = night_c if dark else day_c
    img[ground] = [10, 14, 10] if dark else [60, 95, 50]
    # Clouds on a coarse grid, then blown up.
    gh, gw = 18, 32
    ga, gz = pixel_alt_az(alt, az, fov, gw, gh)
    cloud = np.array(
        [[scene.cloudy(float(ga[i, j]), float(gz[i, j]), at) for j in range(gw)] for i in range(gh)]
    )
    cmask = (
        np.asarray(Image.fromarray((cloud * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR))
        > 127
    )
    cmask &= ~(blocked | ground)
    img[cmask] = (
        img[cmask] * 0.3 + (np.array([90, 90, 95]) if dark else np.array([200, 200, 205])) * 0.7
    )
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).resize(OUT, Image.BILINEAR)
    if camera == "tele":
        out = out.filter(ImageFilter.GaussianBlur(1))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=80)
    return buf.getvalue()
