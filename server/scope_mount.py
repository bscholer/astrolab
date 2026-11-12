"""Fit how the scope's motor angles map to the sky, from slews where both are known.

The mount is modelled as a pitch axis and a rotation axis, set up at some unknown tilt and
heading. Pitch is taken as the angle from the mount's own zenith, with an unknown sign and
zero, and rotation as the turn about that zenith, with an unknown sign. A 3D rotation then
takes the mount's frame to the local sky (east, north, up). The sign and zero are found by
search, and the rotation by the Kabsch method, so no solver library is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from server import sky

MIN_PAIRS = 3
OUTLIER_FACTOR = 3.0
OUTLIER_FLOOR_DEG = 2.0
GOOD_RMS_DEG = 3.0


def enu(alt_deg, az_deg) -> np.ndarray:
    """Unit vectors in east, north, up. Azimuth counts from north toward east."""
    alt, az = np.radians(alt_deg), np.radians(az_deg)
    return np.stack([np.cos(alt) * np.sin(az), np.cos(alt) * np.cos(az), np.sin(alt)], -1)


def alt_az(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    v = v / np.linalg.norm(v, axis=-1, keepdims=True)
    alt = np.degrees(np.arcsin(np.clip(v[..., 2], -1, 1)))
    az = np.degrees(np.arctan2(v[..., 0], v[..., 1])) % 360
    return alt, az


def _mount_vectors(theta_deg, phi_deg) -> tuple[np.ndarray, np.ndarray]:
    """Pointing and the direction of falling theta, toward the axis, in the mount's frame."""
    t, p = np.radians(theta_deg), np.radians(phi_deg)
    d = np.stack([np.sin(t) * np.cos(p), np.sin(t) * np.sin(p), np.cos(t)], -1)
    up = -np.stack([np.cos(t) * np.cos(p), np.cos(t) * np.sin(p), -np.sin(t)], -1)
    return d, up


def _kabsch(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The proper rotation r that best takes rows of a onto rows of b, so b ~ a @ r.T."""
    u, _s, vt = np.linalg.svd(a.T @ b)
    flip = np.sign(np.linalg.det(u @ vt))
    return (u @ np.diag([1.0, 1.0, flip]) @ vt).T


def _errors_deg(r: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    cos = np.clip(np.sum((a @ r.T) * b, axis=1), -1, 1)
    return np.degrees(np.arccos(cos))


@dataclass(frozen=True)
class MountModel:
    pitch_sign: float
    pitch_zero: float
    rotation_sign: float
    matrix: tuple[tuple[float, ...], ...]
    rms_deg: float
    max_deg: float
    used: int
    total: int
    # The miss for each input pair in order, None for a pair that could not be used.
    errors_deg: tuple[float | None, ...] = field(default=())

    @property
    def good(self) -> bool:
        return self.used >= MIN_PAIRS and self.rms_deg <= GOOD_RMS_DEG

    def _theta_phi(self, pitch, rotation):
        theta = self.pitch_sign * np.asarray(pitch, float) + self.pitch_zero
        return theta, self.rotation_sign * np.asarray(rotation, float)

    def vectors(self, pitch, rotation) -> tuple[np.ndarray, np.ndarray]:
        """Pointing and image-up as sky (east, north, up) vectors.

        Raising the pitch moves the view toward the top of the frame, so image-up follows
        rising pitch. That holds on a level tripod and on a tilted mount alike.
        """
        d, toward_axis = _mount_vectors(*self._theta_phi(pitch, rotation))
        r = np.array(self.matrix)
        return d @ r.T, -self.pitch_sign * toward_axis @ r.T

    def to_sky(self, pitch, rotation) -> tuple[np.ndarray, np.ndarray]:
        """Altitude and azimuth, in degrees, of where the motors point."""
        return alt_az(self.vectors(pitch, rotation)[0])

    def summary(self) -> dict:
        return {
            "good": self.good,
            "used": self.used,
            "total": self.total,
            "rms_deg": round(self.rms_deg, 2),
            "max_deg": round(self.max_deg, 2),
            "pitch_sign": self.pitch_sign,
            "pitch_zero_deg": round(self.pitch_zero, 2),
            "rotation_sign": self.rotation_sign,
        }


def pair_alt_az(pair: dict, loc) -> tuple[float, float] | None:
    """The true altitude and azimuth of a pair, worked out from its RA, Dec and time if needed."""
    if pair.get("alt_deg") is not None and pair.get("az_deg") is not None:
        return float(pair["alt_deg"]), float(pair["az_deg"])
    if loc is None or pair.get("ra_deg") is None or pair.get("t") is None:
        return None
    at = datetime.fromtimestamp(float(pair["t"]), UTC)
    return sky.alt_az_now(float(pair["ra_deg"]), float(pair["dec_deg"]), loc, at)


def _search(motors: np.ndarray, b: np.ndarray):
    """Best (error sum, signs, zero, rotation) over pitch sign, rotation sign, and pitch zero."""
    best = None
    for ps in (1.0, -1.0):
        for rs in (1.0, -1.0):
            zeros = np.arange(-180.0, 180.0, 1.0)
            for step in (1.0, 0.1, 0.01):
                scored = []
                for z in zeros:
                    a, _ = _mount_vectors(ps * motors[:, 0] + z, rs * motors[:, 1])
                    r = _kabsch(a, b)
                    scored.append((float(np.sum(_errors_deg(r, a, b) ** 2)), z, r))
                cost, z, r = min(scored, key=lambda s: s[0])
                zeros = np.arange(z - step, z + step, step / 10)
            if best is None or cost < best[0]:
                best = (cost, ps, rs, z, r)
    return best


def fit(pairs: list[dict], loc=None) -> MountModel | None:
    """Fit a mount model. Drops pairs far off the first fit, such as ones from before a move."""
    rows, index = [], []
    for i, p in enumerate(pairs):
        if p.get("pitch") is None or p.get("rotation") is None:
            continue
        aa = pair_alt_az(p, loc)
        if aa is not None:
            rows.append((float(p["pitch"]), float(p["rotation"]), *aa))
            index.append(i)
    if len(rows) < MIN_PAIRS:
        return None
    data = np.array(rows)
    sky_all = enu(data[:, 2], data[:, 3])
    keep = np.ones(len(data), dtype=bool)
    for _ in range(3):
        _cost, ps, rs, z, r = _search(data[keep, :2], sky_all[keep])
        a_all, _ = _mount_vectors(ps * data[:, 0] + z, rs * data[:, 1])
        err = _errors_deg(r, a_all, sky_all)
        limit = max(OUTLIER_FLOOR_DEG, OUTLIER_FACTOR * float(np.median(err[keep])))
        new_keep = err <= limit
        if new_keep.sum() < MIN_PAIRS or (new_keep == keep).all():
            break
        keep = new_keep
    else:
        _cost, ps, rs, z, r = _search(data[keep, :2], sky_all[keep])
        a_all, _ = _mount_vectors(ps * data[:, 0] + z, rs * data[:, 1])
        err = _errors_deg(r, a_all, sky_all)
    used = err[keep]
    per_pair: list[float | None] = [None] * len(pairs)
    for i, e in zip(index, err, strict=True):
        per_pair[i] = round(float(e), 2)
    return MountModel(
        pitch_sign=ps,
        pitch_zero=float(z),
        rotation_sign=rs,
        matrix=tuple(tuple(float(x) for x in row) for row in r),
        rms_deg=float(np.sqrt(np.mean(used**2))),
        max_deg=float(used.max()),
        used=int(keep.sum()),
        total=len(data),
        errors_deg=tuple(per_pair),
    )
