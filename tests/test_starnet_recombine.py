"""The Stars control: shrinking the StarNet stars layer on the way back in."""

from __future__ import annotations

import numpy as np

from nodes.basic.starnet_recombine import StarnetRecombineParams, shrink_stars


def _star() -> np.ndarray:
    y, x = np.mgrid[-10:11, -10:11]
    return (0.9 * np.exp(-(x**2 + y**2) / (2 * 2.5**2))).astype(np.float32)


def test_one_is_identity_and_zero_is_starless() -> None:
    s = _star()
    assert shrink_stars(s, 1.0) is s
    assert not shrink_stars(s, 0.0).any()


def test_lower_amount_shrinks_wings_more_than_core() -> None:
    s = _star()
    out = shrink_stars(s, 0.5)
    core = out[10, 10] / s[10, 10]
    wing = out[10, 16] / s[10, 16]
    assert core > 0.85
    assert wing < 0.3
    # The visible footprint (above a faint threshold) gets smaller.
    assert (out > 0.05).sum() < (s > 0.05).sum()


def test_default_keeps_stars() -> None:
    assert StarnetRecombineParams().stars == 1.0
