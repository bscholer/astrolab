"""The numpy finishing steps behind the instant preview and the last touches."""

from __future__ import annotations

import numpy as np

from server import finish_math as fm


def _sky(seed: int = 0, shape=(3, 64, 64)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (0.01 + 0.002 * rng.standard_normal(shape)).clip(1e-4, 1).astype(np.float32)


def test_mtf_maps_the_balance_point_to_half() -> None:
    assert abs(fm.mtf(0.2, 0.2) - 0.5) < 1e-6
    assert fm.mtf(0.0, 0.3) == 0 and abs(fm.mtf(1.0, 0.3) - 1) < 1e-6


def test_autostretch_puts_the_sky_at_the_target() -> None:
    data = _sky()
    black, mid = fm.autostretch_params(data, -2.8, 0.25, linked=True)
    out = fm.apply_mtf(data, black, mid)
    assert abs(float(np.median(out)) - 0.25) < 0.02
    assert black[0] == black[1] == black[2]


def test_scnr_holds_green_to_the_red_blue_mean_and_keeps_lightness() -> None:
    data = np.zeros((3, 4, 4), np.float32)
    data[0], data[1], data[2] = 0.2, 0.6, 0.2
    out = fm.scnr(data)
    assert np.allclose(out[1], out[0])
    assert np.allclose(out.mean(axis=0), data.mean(axis=0), atol=1e-5)


def test_bp_shift_clips_the_fraction_and_keeps_nodata() -> None:
    data = _sky(1)
    data[:, :4, :] = 0
    out = fm.bp_shift(data, 0.05, neutral_sky=False)
    lit = (data != 0).any(axis=0)
    assert np.isclose((out[:, lit] == 0).mean(), 0.05, atol=0.02)
    assert (out[:, ~lit] == 0).all()


def test_tone_and_sharpen_are_no_ops_at_zero_and_keep_black_and_white() -> None:
    img = np.linspace(0, 1, 3 * 32 * 32, dtype=np.float32).reshape(3, 32, 32)
    assert fm.tone(img) is img and fm.sharpen(img, 0) is img
    ramp = np.stack([np.linspace(0, 1, 101, dtype=np.float32)] * 3)[:, None, :]
    up = fm.tone(ramp, contrast=0.5)
    assert up[0, 0, 0] == 0 and abs(up[0, 0, -1] - 1) < 1e-6
    assert up[0, 0, 80] > ramp[0, 0, 80] and up[0, 0, 20] < ramp[0, 0, 20]
    lifted = fm.tone(ramp, shadows=1.0)
    assert lifted[0, 0, 30] > ramp[0, 0, 30]


def test_split_stars_takes_a_point_star_out_and_keeps_the_background() -> None:
    img = np.full((3, 21, 21), 0.1, np.float32)
    img[:, 10, 10] = 0.9
    starless, stars = fm.split_stars(img, size=1.0)
    assert abs(float(starless[0, 10, 10]) - 0.1) < 1e-6
    assert abs(float(stars[0, 10, 10]) - 0.8) < 1e-6
    assert np.allclose(starless[:, 0, 0], 0.1)


def test_tighten_shrinks_a_star_layer() -> None:
    stars = np.zeros((3, 9, 9), np.float32)
    stars[:, 3:6, 3:6] = 0.5
    stars[:, 4, 4] = 1.0
    tight = fm.tighten_stars(stars, 1.0)
    assert tight[0, 3, 3] < stars[0, 3, 3] and tight.sum() < stars.sum()
