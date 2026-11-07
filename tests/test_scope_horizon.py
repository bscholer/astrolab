"""Sky mask: smooth blue sky counts, textured ground does not, and a pale wall apart from sky does not."""

from __future__ import annotations

import numpy as np
from PIL import Image

from server.scope_horizon import sky_mask


def _scene() -> Image.Image:
    rng = np.random.default_rng(1)
    a = np.zeros((180, 320, 3), dtype=np.uint8)
    a[:90, :] = (110, 150, 215)
    a[90:, :] = rng.integers(20, 120, size=(90, 320, 3))
    a[100:170, 10:120] = (160, 155, 145)
    return Image.fromarray(a)


def test_blue_sky_is_marked_and_ground_is_not():
    mask = sky_mask(_scene())
    assert mask[:80].mean() > 0.95
    assert mask[110:].mean() < 0.05


def test_pale_wall_that_does_not_touch_the_sky_stays_out():
    mask = sky_mask(_scene())
    assert not mask[120:160, 30:100].any()
