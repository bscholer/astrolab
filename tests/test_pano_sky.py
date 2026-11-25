"""Sky mask: smooth blue sky counts, textured ground does not, a pale wall apart from the sky
does not; the horizon ignores wires and gives up where the scan did not reach."""

from __future__ import annotations

import numpy as np

from server.pano_sky import GROUND, SKY, UNSEEN, horizon_profile, sky_mask


def _scene() -> np.ndarray:
    rng = np.random.default_rng(1)
    a = np.zeros((180, 320, 3), dtype=np.uint8)
    a[:90, :] = (110, 150, 215)
    a[90:, :] = rng.integers(20, 120, size=(90, 320, 3))
    a[100:170, 10:120] = (160, 155, 145)
    return a


def test_blue_sky_is_marked_and_ground_is_not():
    mask = sky_mask(_scene())
    assert mask[:80].mean() > 0.95
    assert mask[110:].mean() < 0.05


def test_pale_wall_that_does_not_touch_the_sky_stays_out():
    mask = sky_mask(_scene())
    assert not mask[120:160, 30:100].any()


def test_unseen_pixels_are_never_sky():
    valid = np.ones((180, 320), bool)
    valid[:, :40] = False
    assert not sky_mask(_scene(), valid)[:, :40].any()


def _sphere(top_alt_of_ground: float) -> np.ndarray:
    m = np.full((1024, 2048), SKY, np.uint8)
    m[int((90 - top_alt_of_ground) / 180 * 1024) :] = GROUND
    return m


def test_horizon_is_the_top_of_the_ground():
    alt = horizon_profile(_sphere(10.0))
    assert len(alt) == 360 and all(abs(a - 10.0) < 0.5 for a in alt)


def test_a_wire_across_the_sky_is_not_a_horizon():
    m = _sphere(10.0)
    m[300:302, :] = GROUND  # two pixels thick, about a third of a degree
    assert all(abs(a - 10.0) < 0.5 for a in horizon_profile(m))


def test_unseen_azimuths_have_no_horizon_and_open_sky_reports_how_low_it_was_seen():
    m = _sphere(10.0)
    m[:, :57] = UNSEEN  # azimuth 0 to 10
    m[:, 1024:1081] = SKY  # azimuth 180 to 190: open down to the bottom of the scan
    m[700:, 1024:1081] = UNSEEN
    alt = horizon_profile(m)
    assert alt[0] is None and alt[9] is None
    assert -35 < alt[185] < -30


def test_a_patch_floating_in_the_sky_is_not_a_horizon_unless_painted():
    m = _sphere(10.0)
    m[100:200, 500:700] = GROUND  # altitude 72 to 55, not touching the ground
    assert all(abs(a - 10.0) < 0.5 for a in horizon_profile(m))
    keep = np.zeros_like(m, bool)
    keep[100:200, 500:700] = True
    assert horizon_profile(m, keep=keep)[95] > 70
