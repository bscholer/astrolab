"""HOO channel mapping: OIII is matched to H-alpha on the nebula, not the stars."""

from __future__ import annotations

import numpy as np

from nodes.basic.channel_map import hoo


def test_bright_oiii_stars_do_not_turn_the_nebula_down() -> None:
    rng = np.random.default_rng(2)
    h, w = 512, 768
    yy, xx = np.mgrid[0:h, 0:w]
    nebula = np.exp(-((yy - 256) ** 2 + (xx - 384) ** 2) / (2 * 90.0**2)).astype(np.float32)
    stars = np.zeros((h, w), np.float32)
    for y, x in rng.uniform(10, [h - 10, w - 10], (400, 2)):
        stars += np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * 1.5**2)).astype(np.float32)
    sky = 0.01
    ha = sky + 0.02 * nebula + 0.05 * stars
    oiii = sky + 0.01 * nebula + 0.2 * stars  # half the nebula, four times the stars
    rgb = np.stack([ha, oiii, oiii]) + rng.normal(0, 1e-4, (3, h, w)).astype(np.float32)
    out = hoo(rgb, 1.0)
    core = (slice(236, 276), slice(364, 404))
    # Matched on the nebula: the mapped OIII core sits near H-alpha's.
    ratio = (np.median(out[1][core]) - sky) / (np.median(out[0][core]) - sky)
    assert 0.8 < ratio < 1.25
