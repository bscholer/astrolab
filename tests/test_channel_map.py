"""HOO: colour from where OIII is relatively strong, brightness from both channels."""

from __future__ import annotations

import numpy as np

from nodes.basic.channel_map import hoo


def _scene(seed: int = 2):
    rng = np.random.default_rng(seed)
    h, w = 512, 768
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(yy - 256, xx - 384)
    core = np.exp(-(r**2) / (2 * 60.0**2)).astype(np.float32)
    ring = np.exp(-((r - 170) ** 2) / (2 * 40.0**2)).astype(np.float32)
    stars = np.zeros((h, w), np.float32)
    for y, x in rng.uniform(10, [h - 10, w - 10], (300, 2)):
        stars += np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * 1.5**2)).astype(np.float32)
    sky = 0.01
    ha = sky + 0.02 * ring + 0.01 * core + 0.05 * stars
    oiii = sky + 0.002 * ring + 0.012 * core + 0.2 * stars  # OIII in the core; stars bright in it
    rgb = np.stack([ha, oiii, oiii]) + rng.normal(0, 1e-4, (3, h, w)).astype(np.float32)
    return rgb, r


def test_core_comes_out_bluer_than_the_ring() -> None:
    rgb, r = _scene()
    out = hoo(rgb, 1.4)
    core, ring = r < 40, (r > 150) & (r < 190)

    def blue_over_red(m):
        return np.median(out[2][m]) / np.median(out[0][m])

    assert blue_over_red(core) > 1.5 * blue_over_red(ring)
    assert blue_over_red(ring) < 0.8  # the H-alpha ring stays red


def test_balance_turns_more_blue() -> None:
    rgb, r = _scene()
    mid = (r > 80) & (r < 140)
    lo, hi = hoo(rgb, 0.6), hoo(rgb, 1.8)
    assert np.median(hi[2][mid] / hi[0][mid]) > np.median(lo[2][mid] / lo[0][mid])


def test_zero_border_stays_zero() -> None:
    rgb, _ = _scene()
    rgb[:, :20, :] = 0
    out = hoo(rgb, 1.4)
    assert not out[:, :20, :].any()
