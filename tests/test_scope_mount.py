"""Mount fit: recover a tilted, turned mount from motor angles and true sky positions."""

from __future__ import annotations

import numpy as np
import pytest

from server import scope_horizon, scope_mount
from server.scope_mount import MountModel, enu, fit


def _rot(axis: str, deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def _true_model(pitch_sign=-1.0, pitch_zero=90.0, rotation_sign=-1.0, tilt=(4.0, -3.0, 37.0)):
    r = _rot("z", tilt[2]) @ _rot("y", tilt[1]) @ _rot("x", tilt[0])
    m = tuple(tuple(row) for row in r)
    return MountModel(pitch_sign, pitch_zero, rotation_sign, m, 0.0, 0.0, 0, 0)


def _pairs(model, motors, noise_deg=0.0, seed=1):
    rng = np.random.default_rng(seed)
    out = []
    for pitch, rot in motors:
        alt, az = model.to_sky(pitch, rot)
        out.append(
            {
                "pitch": pitch,
                "rotation": rot,
                "alt_deg": float(alt) + rng.normal(0, noise_deg),
                "az_deg": float(az) + rng.normal(0, noise_deg),
            }
        )
    return out


MOTORS = [(45, 10), (40, 100), (50, 190), (45, 280), (70, 150), (30, 330), (60, 60)]


@pytest.mark.parametrize("pitch_sign", [1.0, -1.0])
@pytest.mark.parametrize("rotation_sign", [1.0, -1.0])
def test_recovers_the_mapping(pitch_sign, rotation_sign):
    true = _true_model(pitch_sign, 90.0 if pitch_sign < 0 else 0.0, rotation_sign)
    motors = [(90 - p if pitch_sign > 0 else p, r) for p, r in MOTORS]
    model = fit(_pairs(true, motors, noise_deg=0.1))
    assert model is not None and model.good
    assert model.rms_deg < 0.5
    for pitch, rot in [(20, 0), (80, 222), (35, 123), (55, 300)]:
        p = 90 - pitch if pitch_sign > 0 else pitch
        a = enu(*true.to_sky(p, rot))
        b = enu(*model.to_sky(p, rot))
        assert np.degrees(np.arccos(np.clip(a @ b, -1, 1))) < 0.5


def test_image_up_points_toward_the_zenith():
    true = _true_model(tilt=(0.0, 0.0, 10.0))
    model = fit(_pairs(true, MOTORS))
    d, up = model.vectors(30.0, 77.0)
    assert up[2] > 0.8
    assert abs(float(d @ up)) < 1e-9


def test_drops_a_pair_from_before_the_scope_moved():
    true = _true_model()
    pairs = _pairs(true, MOTORS, noise_deg=0.1)
    pairs[2]["az_deg"] += 40
    model = fit(pairs)
    assert model.good and model.used == len(MOTORS) - 1
    assert model.errors_deg[2] > 20


def test_needs_three_pairs():
    assert fit(_pairs(_true_model(), MOTORS[:2])) is None
    assert fit([{"pitch": None, "rotation": 3, "alt_deg": 1, "az_deg": 2}] * 5) is None


def _tree_line(az):
    """A house at azimuth 90 to 150 up to 40 degrees, trees elsewhere at 15 to 25."""
    return np.where((az > 90) & (az < 150), 40.0, 20.0 + 5.0 * np.sin(np.radians(3 * az)))


def _tiles(model, pitches, rotations):
    tiles = []
    for pitch in pitches:
        for rot in rotations:
            rays = scope_horizon.pixel_directions(model, pitch, rot, 180, 320)
            alt, az = scope_mount.alt_az(rays)
            tiles.append((pitch, rot, alt > _tree_line(az)))
    return tiles


def test_horizon_line_follows_the_mask():
    true = _true_model(tilt=(2.0, -1.0, 80.0))
    model = fit(_pairs(true, MOTORS, noise_deg=0.2))
    tiles = _tiles(true, (10.0, 35.0, 60.0), tuple(float(r) for r in range(0, 360, 45)))
    profile = scope_horizon._fill_gaps(
        scope_horizon.horizon_profile(*scope_horizon.vote_grid(tiles, model))
    )
    assert all(v is not None for v in profile)
    for az in (0, 45, 120, 200, 300):
        want = float(_tree_line(np.array(az + 1.0)))
        got = scope_horizon.horizon_at(profile, az)
        assert want - 1 <= got <= want + 5, (az, want, got)


def test_unseen_azimuths_stay_unknown():
    profile = [None] * scope_horizon.AZ_BINS
    profile[0] = 10.0
    profile[3] = 30.0
    filled = scope_horizon._fill_gaps(profile)
    assert filled[1] == filled[2] == 30.0
    assert filled[50] is None
    assert scope_horizon.horizon_at(filled, 1.0 * scope_horizon.CELL_DEG) == 30.0
