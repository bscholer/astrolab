"""What more data buys: measuring a master, advice, and raising scheduler goals."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from server.capture_loop import Quality, advise, measure, noise_drop, queue_more


def test_noise_falls_with_the_square_root_of_time() -> None:
    assert noise_drop(3600, 3 * 3600) == pytest.approx(0.5)
    assert noise_drop(0, 3600) == 1.0


def test_advice_verdicts() -> None:
    q = Quality(sky=0.01, noise=0.001, signal=0.02)
    short = advise(0.5 * 3600, q)
    assert short.verdict == "worth_it"
    assert short.headline == "About 2 more hours would cut noise by 55%"
    assert short.options[1]["snr"] == pytest.approx(10 / (1 - short.options[1]["noise_drop"]))
    assert advise(5 * 3600, q).verdict == "slowing"
    assert advise(5 * 3600, q).headline == "6.2 hours more would cut noise by a third"
    long = advise(40 * 3600, None)
    assert long.verdict == "diminishing" and long.snr is None
    assert long.headline.startswith("Diminishing returns: 10 more hours for 11%")


def test_measure_ignores_gradients(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    h, w = 512, 768
    ramp = np.linspace(0.0, 0.02, w, dtype=np.float32)[None, :]  # light pollution
    img = 0.01 + ramp + rng.normal(0, 0.001, (h, w)).astype(np.float32)
    img[200:300, 300:450] += 0.01  # a galaxy
    p = tmp_path / "stack.fit"
    fits.PrimaryHDU(np.stack([img] * 3)).writeto(p)
    q = measure(p)
    assert q.noise == pytest.approx(0.001, rel=0.1)  # a plain MAD would see the ramp
    assert q.signal > q.sky + 0.005


def test_queue_more_raises_or_adds() -> None:
    targets = [{"name": "M 33", "goal_hours": 3.0, "ra_deg": 23.4, "dec_deg": 30.6}]
    out, t = queue_more(targets, {"M 33": 4 * 3600}, name="M 33", more_h=2, ra_deg=None,
                        dec_deg=None, object_type=None, filter_name="Astro", exposure_s=30,
                        common_name=None)
    assert t["goal_hours"] == 6.0  # past its goal: what's shot plus two
    assert targets[0]["goal_hours"] == 3.0  # the input list is left alone
    out, t = queue_more(out, {}, name="NGC 1499", more_h=2, ra_deg=60.8, dec_deg=36.4,
                        object_type="Nebula", filter_name="HaOIII", exposure_s=15,
                        common_name="California Nebula")
    assert len(out) == 2
    assert (t["filter"], t["exposure_s"], t["goal_hours"]) == ("duo", 15, 2.0)
    with pytest.raises(ValueError):
        queue_more([], {}, name="Nowhere", more_h=1, ra_deg=None, dec_deg=None,
                   object_type=None, filter_name=None, exposure_s=30, common_name=None)
