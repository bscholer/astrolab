"""Stitch the wide-camera scan into one equirectangular panorama.

The camera only turns, so every frame is a rotation of one pinhole camera. The motor angles
give a first guess of each rotation. Features matched between overlapping frames then refine
the rotations, the focal length and the lens in one least-squares fit (a bundle adjustment),
with the motor guess as a soft prior so frames of plain sky stay where the motors put them.

The mount is equatorial, so its rotation axis points at the celestial pole, not straight up.
Up is found from vertical lines (fence boards, house corners, trunks): their great circles all
meet at the zenith. The latitude says roughly where to look, and the bright sky says which
way is up. If the mount is polar aligned, the axis also points north, which gives the compass.

Measured on the real scope (scan 7196d0c4, November 2026): f is about 1500 px at 1280x720, so
the frame is 46 x 27 degrees. The camera sits 195.2 - pitch degrees from the rotation axis.
Close things (the house, the fence) shift a few pixels between frames because the camera is
not on the axes, so the fit uses a robust loss and the blend keeps seams narrow.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

# The camera model, in degrees and pixels at 1280 wide.
FOCAL_PX = 1500.0
AXIS_OFFSET_DEG = 195.2
REF_WIDTH = 1280

BLANK_LAPLACIAN = 8.0  # below this the frame has no detail: plain wall, base, or a cap
BLANK_WEIGHT = 0.02
MAX_PAIR_SEP_DEG = 70.0  # loose: the geometry is not fitted yet when pairs are picked
RATIO = 0.75
RANSAC_PX = 4.0
MIN_INLIERS = 12
MAX_MATCHES_PER_PAIR = 80
PRIOR_SIGMA_DEG = 3.0
MATCH_SIGMA_PX = 2.0
MAX_PRIOR_DISAGREEMENT_DEG = 10.0
MAX_BRIGHTNESS_SAMPLES = 20000
POLAR_TOLERANCE_DEG = 10.0
CLEAR_LIFT = 15.0  # grey levels brighter above than below, for "there is a bright side"
UP_CONE_DEG = 40.0


@dataclass(frozen=True)
class Lens:
    width: int = 1280
    height: int = 720
    f: float = FOCAL_PX
    k1: float = 0.0
    axis_offset: float = AXIS_OFFSET_DEG
    pitch_scale: float = 1.0
    yaw: float = 0.0
    roll: float = 0.0

    @classmethod
    def for_size(cls, width: int, height: int) -> Lens:
        return cls(width=width, height=height, f=FOCAL_PX * width / REF_WIDTH)

    @property
    def fov_deg(self) -> tuple[float, float]:
        return (
            math.degrees(2 * math.atan(self.width / 2 / self.f)),
            math.degrees(2 * math.atan(self.height / 2 / self.f)),
        )


@dataclass
class Tile:
    name: str
    pitch: float
    rotation: float


# --- rotations ---------------------------------------------------------------------------


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _ry(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def _rx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


# Camera axes (x right, y down, z forward) in the mount frame when the camera looks along the
# rotation axis (+Z). Image up then points toward -X, so tilting away from the axis toward +X
# moves the view down the frame, as raising the pitch moves it up.
_C0 = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])


def motor_rotation(pitch: float, rotation: float, lens: Lens) -> np.ndarray:
    """Camera-to-mount rotation for motor angles. The mount's Z is its rotation axis.

    The camera sits pitch_scale * (axis_offset - pitch) degrees from the axis. Both depend on
    how the scope was set up: on the equatorial mount they were 195 and 0.98, on a level
    tripod after a prepare 187 and 0.85. So every scan fits them from its own frames.
    """
    t = math.radians(lens.pitch_scale * (lens.axis_offset - pitch))
    trim = _ry(math.radians(lens.yaw)) @ _rz(math.radians(lens.roll))
    return _rz(math.radians(rotation)) @ _ry(t) @ _C0 @ trim


def rodrigues(w: np.ndarray) -> np.ndarray:
    return cv2.Rodrigues(np.asarray(w, float).reshape(3, 1))[0]


def _skew(v: np.ndarray) -> np.ndarray:
    """Skew matrices for rows of v, shape (n, 3, 3)."""
    z = np.zeros(len(v))
    return np.stack(
        [
            np.stack([z, -v[:, 2], v[:, 1]], -1),
            np.stack([v[:, 2], z, -v[:, 0]], -1),
            np.stack([-v[:, 1], v[:, 0], z], -1),
        ],
        -2,
    )


# --- pixels and rays ---------------------------------------------------------------------


def pixel_rays(uv: np.ndarray, lens: Lens) -> np.ndarray:
    x = (uv[:, 0] - lens.width / 2) / lens.f
    y = (uv[:, 1] - lens.height / 2) / lens.f
    s = 1 + lens.k1 * (x * x + y * y)
    v = np.stack([x * s, y * s, np.ones_like(x)], -1)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def project(rays: np.ndarray, lens: Lens) -> tuple[np.ndarray, np.ndarray]:
    """Pixel coordinates of camera-frame rays, and whether each lands near the frame.

    Rays well outside the frame are marked off before the lens term is undone: for them the
    inversion below does not converge and can land inside the frame by accident.
    """
    z = rays[..., 2]
    zs = np.where(z > 1e-6, z, 1.0)
    x, y = rays[..., 0] / zs, rays[..., 1] / zs
    front = (z > 1e-6) & (np.abs(x) < 0.75 * lens.width / lens.f)
    front &= np.abs(y) < 0.75 * lens.height / lens.f
    x, y = np.where(front, x, 0.0), np.where(front, y, 0.0)
    s = np.ones_like(x)
    for _ in range(4):  # undo the radial term: solve xd * (1 + k1 r_d^2) = x
        xd, yd = x / s, y / s
        s = 1 + lens.k1 * (xd * xd + yd * yd)
    u = x / s * lens.f + lens.width / 2
    v = y / s * lens.f + lens.height / 2
    return np.stack([u, v], -1), front


def _kabsch(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotations r with b ~ r @ a, batched over the leading axis. a, b: (..., n, 3)."""
    m = np.swapaxes(b, -1, -2) @ a
    u, _s, vt = np.linalg.svd(m)
    d = np.sign(np.linalg.det(u @ vt))
    fix = np.ones(u.shape[:-2] + (3,))
    fix[..., 2] = d
    return (u * fix[..., None, :]) @ vt


def _angle_deg(r: np.ndarray) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(r) - 1) / 2))))


# --- frames ------------------------------------------------------------------------------


def is_blank(gray: np.ndarray) -> bool:
    small = cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(small, cv2.CV_64F).var()) < BLANK_LAPLACIAN


@dataclass
class Pair:
    i: int
    j: int
    ui: np.ndarray  # pixels in frame i, (n, 2)
    uj: np.ndarray
    rel: np.ndarray  # measured rotation taking i's camera rays to j's
    inliers: int


def _features(gray: np.ndarray):
    sift = cv2.SIFT_create(nfeatures=2500)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    kp, desc = sift.detectAndCompute(clahe.apply(gray), None)
    pts = np.array([k.pt for k in kp], float).reshape(-1, 2)
    return pts, desc


def _ransac_rotation(ra: np.ndarray, rb: np.ndarray, thr_rad: float, rng, iters: int = 400):
    """Best rotation taking rays a to rays b, from two-point samples. Returns (mask, r)."""
    n = len(ra)
    idx = np.stack([rng.choice(n, 2, replace=False) for _ in range(iters)])
    rs = _kabsch(ra[idx], rb[idx])  # (iters, 3, 3)
    pred = np.einsum("kij,nj->kni", rs, ra)
    err = np.linalg.norm(pred - rb[None], axis=-1)
    counts = (err < thr_rad).sum(1)
    best = int(np.argmax(counts))
    mask = err[best] < thr_rad
    if mask.sum() >= 3:
        r = _kabsch(ra[mask], rb[mask])
        mask = np.linalg.norm(ra @ r.T - rb, axis=-1) < thr_rad
        if mask.sum() >= 3:
            r = _kabsch(ra[mask], rb[mask])
        return mask, r
    return mask, rs[best]


def match_pairs(
    grays: list[np.ndarray],
    priors: list[np.ndarray],
    lens: Lens,
    seed: int = 0,
    skip: set[int] | frozenset[int] = frozenset(),
) -> list[Pair]:
    """Matches between frames the motors say may overlap, kept where one rotation fits them."""
    rng = np.random.default_rng(seed)
    feats = [(np.zeros((0, 2)), None) if k in skip else _features(g) for k, g in enumerate(grays)]
    centers = np.array([p[:, 2] for p in priors])
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    pairs = []
    for i in range(len(grays)):
        for j in range(i + 1, len(grays)):
            sep = math.degrees(math.acos(np.clip(centers[i] @ centers[j], -1, 1)))
            if sep > MAX_PAIR_SEP_DEG:
                continue
            (pi, di), (pj, dj) = feats[i], feats[j]
            if di is None or dj is None or len(di) < MIN_INLIERS or len(dj) < MIN_INLIERS:
                continue
            knn = matcher.knnMatch(di, dj, k=2)
            good = [m[0] for m in knn if len(m) == 2 and m[0].distance < RATIO * m[1].distance]
            if len(good) < MIN_INLIERS:
                continue
            ui = pi[[m.queryIdx for m in good]]
            uj = pj[[m.trainIdx for m in good]]
            ri, rj = pixel_rays(ui, lens), pixel_rays(uj, lens)
            mask, rel = _ransac_rotation(ri, rj, RANSAC_PX / lens.f, rng)
            if mask.sum() < MIN_INLIERS:
                continue
            keep = np.flatnonzero(mask)
            if len(keep) > MAX_MATCHES_PER_PAIR:
                keep = rng.choice(keep, MAX_MATCHES_PER_PAIR, replace=False)
            pairs.append(Pair(i, j, ui[keep], uj[keep], rel, int(mask.sum())))
    return pairs


def calibrate_mount(tiles: list[Tile], pairs: list[Pair], lens: Lens) -> tuple[Lens, float]:
    """Fit axis_offset and pitch_scale so the motors predict the measured pair rotations.

    A grid search, so a geometry far from the last scope setup is still found. Returns the
    lens and the share of pairs that then agree within MAX_PRIOR_DISAGREEMENT_DEG.
    """
    if not pairs:
        return lens, 0.0
    offsets = np.arange(120.0, 240.01, 1.0)
    scales = np.arange(0.70, 1.2001, 0.01)
    pitch = np.array([t.pitch for t in tiles])
    rz = np.array([_rz(math.radians(t.rotation)) for t in tiles])
    ii = np.array([p.i for p in pairs])
    jj = np.array([p.j for p in pairs])
    rel = np.array([p.rel for p in pairs])
    wt = np.sqrt([p.inliers for p in pairs])
    # Measured rel in mount terms: C rel C^T. Pred: Ry_j^T Rz_j^T Rz_i Ry_i (C cancels).
    meas = _C0 @ rel @ _C0.T
    zz = np.einsum("pji,pjk->pik", rz[jj], rz[ii])  # Rz_j^T Rz_i
    tol = math.radians(MAX_PRIOR_DISAGREEMENT_DEG)
    best = (-1.0, float(lens.axis_offset), float(lens.pitch_scale))
    for sc in scales:
        t = np.radians(sc * (offsets[:, None] - pitch[None, :]))  # (offsets, tiles)
        c, s_ = np.cos(t), np.sin(t)
        ry = np.zeros(t.shape + (3, 3))
        ry[..., 0, 0], ry[..., 0, 2], ry[..., 1, 1] = c, s_, 1.0
        ry[..., 2, 0], ry[..., 2, 2] = -s_, c
        pred = np.einsum("opji,pjk,opkl->opil", ry[:, jj], zz, ry[:, ii])
        cos = (np.einsum("opij,pij->op", pred, meas) - 1) / 2
        err = np.arccos(np.clip(cos, -1, 1))
        score = (wt * np.maximum(0.0, 1 - (err / tol) ** 2)).sum(1)
        k = int(np.argmax(score))
        if score[k] > best[0]:
            best = (float(score[k]), float(offsets[k]), float(sc))
    fitted = replace(lens, axis_offset=best[1], pitch_scale=best[2])
    agree = np.mean(
        [_pair_disagreement(p, tiles, fitted) <= MAX_PRIOR_DISAGREEMENT_DEG for p in pairs]
    )
    return fitted, float(agree)


def _pair_disagreement(p: Pair, tiles: list[Tile], lens: Lens) -> float:
    ri = motor_rotation(tiles[p.i].pitch, tiles[p.i].rotation, lens)
    rj = motor_rotation(tiles[p.j].pitch, tiles[p.j].rotation, lens)
    return _angle_deg(p.rel @ (rj.T @ ri).T)


# --- bundle adjustment -------------------------------------------------------------------

_GLOBALS = ("f", "k1", "axis_offset", "yaw", "roll", "pitch_scale")
_GLOBAL_SIGMA = {
    "f": 0.15,
    "k1": 0.1,
    "axis_offset": 3.0,
    "yaw": 3.0,
    "roll": 3.0,
    "pitch_scale": 0.03,
}


def _lens_with(lens: Lens, g: np.ndarray, lens0: Lens) -> Lens:
    return replace(
        lens,
        f=lens0.f * math.exp(g[0]),
        k1=lens0.k1 + g[1],
        axis_offset=lens0.axis_offset + g[2],
        yaw=lens0.yaw + g[3],
        roll=lens0.roll + g[4],
        pitch_scale=lens0.pitch_scale + g[5],
    )


@dataclass
class Fit:
    lens: Lens
    rotations: list[np.ndarray]
    rms_px: float
    median_px: float
    matches: int
    pairs: int


def bundle_adjust(tiles: list[Tile], pairs: list[Pair], lens0: Lens, iters: int = 25) -> Fit:
    """Refine every frame's rotation and the shared lens from matches, keeping near the motors."""
    n = len(tiles)
    ii = np.concatenate([np.full(len(p.ui), p.i) for p in pairs]) if pairs else np.zeros(0, int)
    jj = np.concatenate([np.full(len(p.uj), p.j) for p in pairs]) if pairs else np.zeros(0, int)
    ui = np.concatenate([p.ui for p in pairs]) if pairs else np.zeros((0, 2))
    uj = np.concatenate([p.uj for p in pairs]) if pairs else np.zeros((0, 2))
    m = len(ii)
    sig_w = math.radians(PRIOR_SIGMA_DEG)
    gsig = np.array([_GLOBAL_SIGMA[k] for k in _GLOBALS])

    def state(w, g):
        lens = _lens_with(lens0, g, lens0)
        rots = [
            rodrigues(w[k]) @ motor_rotation(t.pitch, t.rotation, lens) for k, t in enumerate(tiles)
        ]
        return lens, np.array(rots)

    def match_residuals(w, g):
        lens, rots = state(w, g)
        vi = np.einsum("nij,nj->ni", rots[ii], pixel_rays(ui, lens))
        vj = np.einsum("nij,nj->ni", rots[jj], pixel_rays(uj, lens))
        return (vi - vj) * lens.f / MATCH_SIGMA_PX, vi, vj, lens

    w = np.zeros((n, 3))
    g = np.zeros(len(_GLOBALS))
    lam = 1e-3

    def cost(w, g):
        r = match_residuals(w, g)[0]
        e2 = (r * r).sum(1)
        robust = np.log1p(e2 / 9.0).sum() * 9.0  # Cauchy, scale 3 sigma
        return robust + ((w / sig_w) ** 2).sum() + ((g / gsig) ** 2).sum()

    current = cost(w, g)
    np_ = 3 * n + len(_GLOBALS)
    for _ in range(iters):
        r, vi, vj, lens = match_residuals(w, g)
        e2 = (r * r).sum(1)
        wt = 1.0 / (1.0 + e2 / 9.0)  # IRLS weights for the Cauchy loss
        scale = lens.f / MATCH_SIGMA_PX
        a = np.zeros((np_, np_))
        b = np.zeros(np_)
        if m:
            # Left perturbation: d(exp(w) R r)/dw = -[R r]x
            ji = -_skew(vi) * scale  # (m, 3, 3)
            jj_ = _skew(vj) * scale
            # Globals by finite differences.
            jg = np.zeros((m, 3, len(_GLOBALS)))
            for k in range(len(_GLOBALS)):
                d = np.zeros_like(g)
                d[k] = 1e-5 if k in (0, 1, 5) else 1e-4
                jg[:, :, k] = (match_residuals(w, g + d)[0] - r) / d[k]
            wr = wt[:, None] * r
            # Block accumulation into the normal equations.
            blocks = {"i": ji, "j": jj_}
            idx = {"i": ii, "j": jj}
            for p in ("i", "j"):
                for q in ("i", "j"):
                    bb = np.einsum("nki,nkj,n->nij", blocks[p], blocks[q], wt)
                    for x in range(3):
                        for y in range(3):
                            np.add.at(a, (3 * idx[p] + x, 3 * idx[q] + y), bb[:, x, y])
                bg = np.einsum("nki,nkj,n->nij", blocks[p], jg, wt)
                for x in range(3):
                    for y in range(len(_GLOBALS)):
                        np.add.at(a[:, 3 * n + y], 3 * idx[p] + x, bg[:, x, y])
                        np.add.at(a[3 * n + y], 3 * idx[p] + x, bg[:, x, y])
                gb = np.einsum("nki,nk->ni", blocks[p], wr)
                for x in range(3):
                    np.add.at(b, 3 * idx[p] + x, gb[:, x])
            a[3 * n :, 3 * n :] += np.einsum("nki,nkj,n->ij", jg, jg, wt)
            b[3 * n :] += np.einsum("nki,nk->i", jg, wr)
        a[np.arange(3 * n), np.arange(3 * n)] += 1 / sig_w**2
        b[: 3 * n] += w.ravel() / sig_w**2
        a[3 * n :, 3 * n :] += np.diag(1 / gsig**2)
        b[3 * n :] += g / gsig**2
        improved = False
        for _ in range(6):
            step = np.linalg.solve(a + lam * np.diag(np.diag(a)), -b)
            w2 = w + step[: 3 * n].reshape(n, 3)
            g2 = g + step[3 * n :]
            c2 = cost(w2, g2)
            if c2 < current:
                w, g, current, lam = w2, g2, c2, max(lam / 3, 1e-7)
                improved = True
                break
            lam *= 10
        if not improved or np.abs(step).max() < 1e-7:
            break

    r, _vi, _vj, lens = match_residuals(w, g)
    err = np.linalg.norm(r, axis=1) * MATCH_SIGMA_PX if m else np.zeros(1)
    _l, rots = state(w, g)
    return Fit(
        lens=lens,
        rotations=list(rots),
        rms_px=float(np.sqrt(np.mean(np.minimum(err, 50) ** 2))),
        median_px=float(np.median(err)),
        matches=m,
        pairs=len(pairs),
    )


# --- exposure ----------------------------------------------------------------------------


def exposure_gains(
    images: list[np.ndarray], rots: list[np.ndarray], lens: Lens, step: int = 32
) -> np.ndarray:
    """Per-frame, per-channel gains that make overlaps agree. The camera exposes each frame.

    Overlaps come from the fitted rotations, not from matched features, so plain sky counts
    too: frames near the Sun come out darker and have nothing to match on.
    """
    n = len(images)
    blurred = [cv2.blur(img, (15, 15)).astype(np.float32) for img in images]
    ys, xs = np.mgrid[step // 2 : lens.height : step, step // 2 : lens.width : step]
    grid = np.stack([xs.ravel(), ys.ravel()], -1).astype(float)
    rays = pixel_rays(grid, lens)
    centers = np.array([r[:, 2] for r in rots])
    gx, gy = grid[:, 0].astype(int), grid[:, 1].astype(int)
    rows, rhs = [], []
    for i in range(n):
        mount = rays @ rots[i].T
        for j in range(i + 1, n):
            if centers[i] @ centers[j] < math.cos(math.radians(MAX_PAIR_SEP_DEG)):
                continue
            uv, front = project(mount @ rots[j], lens)
            inside = front & (uv[:, 0] > 8) & (uv[:, 0] < lens.width - 8)
            inside &= (uv[:, 1] > 8) & (uv[:, 1] < lens.height - 8)
            if inside.sum() < 6:
                continue
            a = blurred[i][gy[inside], gx[inside]]
            u, v = uv[inside].astype(int).T
            b = blurred[j][v, u]
            ok = (a.min(1) > 8) & (b.min(1) > 8) & (a.max(1) < 245) & (b.max(1) < 245)
            if ok.sum() < 6:
                continue
            diff = np.median(np.log(a[ok]) - np.log(b[ok]), axis=0)  # log gain_j - log gain_i
            row = np.zeros(n)
            row[i], row[j] = -1.0, 1.0
            rows.append(row * math.sqrt(ok.sum()))
            rhs.append(diff * math.sqrt(ok.sum()))
    prior = np.eye(n) * 0.5
    a = np.vstack(rows + [prior]) if rows else prior
    b = np.vstack(rhs + [np.zeros((n, 3))]) if rhs else np.zeros((n, 3))
    log_g, *_ = np.linalg.lstsq(a, b, rcond=None)
    log_g -= np.median(log_g, axis=0)
    return np.exp(np.clip(log_g, -1.0, 1.0))


# --- which way is up ---------------------------------------------------------------------


def _fibonacci(n: int) -> np.ndarray:
    k = np.arange(n) + 0.5
    z = 1 - 2 * k / n
    r = np.sqrt(1 - z * z)
    phi = k * math.pi * (3 - math.sqrt(5))
    return np.stack([r * np.cos(phi), r * np.sin(phi), z], -1)


def line_normals(
    grays: list[np.ndarray], rots: list[np.ndarray], lens: Lens
) -> tuple[np.ndarray, np.ndarray]:
    """Great-circle normals (mount frame) and lengths of straight edges in the frames."""
    lsd = cv2.createLineSegmentDetector()
    normals, weights = [], []
    for gray, rot in zip(grays, rots, strict=True):
        segs = lsd.detect(gray)[0]
        if segs is None:
            continue
        segs = segs.reshape(-1, 4)
        length = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
        segs, length = segs[length > lens.width / 25], length[length > lens.width / 25]
        if not len(segs):
            continue
        a = pixel_rays(segs[:, :2], lens) @ rot.T
        b = pixel_rays(segs[:, 2:], lens) @ rot.T
        nrm = np.cross(a, b)
        nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
        normals.append(nrm)
        weights.append(length)
    if not normals:
        return np.zeros((0, 3)), np.zeros(0)
    return np.concatenate(normals), np.concatenate(weights)


@dataclass
class Level:
    up: np.ndarray  # mount frame
    north: np.ndarray | None
    support: float  # share of line length that points at the zenith
    method: str


def estimate_up(
    normals: np.ndarray,
    weights: np.ndarray,
    brightness: tuple[np.ndarray, np.ndarray],
    latitude: float | None,
) -> Level:
    """Zenith from vertical edges, with the brighter side (sky, ceiling lights) as up.

    brightness is (directions, luminance) samples of the frames in the mount frame. Latitude
    does not steer the search, since the scope may sit on a level tripod. It only adds a
    compass when the axis turns out to lean as a polar-aligned axis would.
    """
    axis = np.array([0.0, 0.0, 1.0])
    cand = _fibonacci(20000)
    cand = cand[cand[:, 2] > -0.2]
    tol = math.sin(math.radians(1.0))
    total = weights.sum() or 1.0
    support = np.zeros(len(cand))
    # In chunks: tens of thousands of edges against every candidate would not fit in memory.
    for k in range(0, len(cand), 500):
        part = cand[k : k + 500]
        if len(normals):
            support[k : k + 500] = weights @ (np.abs(normals @ part.T) < tol) / total
    score = support + 1e-3
    dirs, lum = brightness
    if len(dirs):
        if len(dirs) > MAX_BRIGHTNESS_SAMPLES:
            pick = np.random.default_rng(0).choice(len(dirs), MAX_BRIGHTNESS_SAMPLES, replace=False)
            dirs, lum = dirs[pick], lum[pick]
        lift = np.zeros(len(cand))
        for k in range(0, len(cand), 500):
            side = dirs @ cand[k : k + 500].T  # (samples, chunk)
            up_, down = side > 0.1, side < -0.1
            above = lum @ up_ / np.maximum(up_.sum(0), 1)
            below = lum @ down / np.maximum(down.sum(0), 1)
            lift[k : k + 500] = above - below
        bright = cand[int(np.argmax(lift))]
        if lift.max() > CLEAR_LIFT:
            # A clear bright side (daylight, ceiling lights): up lies near it. Siding and fence
            # rails make vanishing points as strong as the vertical one, and only this tells
            # them apart. At night there is no such side and the lines decide alone.
            score = np.where(cand @ bright > math.cos(math.radians(UP_CONE_DEG)), score, 0.0)
        score = score / (1 + np.exp(-lift / 8.0))
    best = cand[int(np.argmax(score))]
    best_support = float(support[int(np.argmax(score))])
    method = "lines"
    if len(normals) and best_support > 0.02:
        inl = np.abs(normals @ best) < 2 * tol
        if inl.sum() >= 2:
            m = (normals[inl] * weights[inl, None]).T @ normals[inl]
            up = np.linalg.eigh(m)[1][:, 0]
            best = up if up @ best > 0 else -up
    else:
        method = "brightness" if len(dirs) else "mount"
        if not len(dirs):
            best = axis
    north = None
    tilt = math.degrees(math.acos(float(np.clip(best @ axis, -1, 1))))
    if latitude is not None and abs(tilt - (90.0 - abs(latitude))) <= POLAR_TOLERANCE_DEG:
        # The axis stands where the pole would: take the mount as polar aligned, so the
        # axis leans toward north (south in the southern hemisphere).
        h = axis - (axis @ best) * best
        if np.linalg.norm(h) > 1e-3:
            north = h / np.linalg.norm(h) * (1 if latitude >= 0 else -1)
    return Level(up=best / np.linalg.norm(best), north=north, support=best_support, method=method)


def world_frame(level: Level) -> np.ndarray:
    """Rows are east, north and up as mount-frame vectors."""
    up = level.up
    north = level.north
    if north is None:  # no compass: pick the mount's X, flattened, as the left edge
        x = np.array([1.0, 0.0, 0.0])
        north = x - (x @ up) * up
        if np.linalg.norm(north) < 1e-3:
            north = np.array([0.0, 1.0, 0.0]) - up[1] * up
        north /= np.linalg.norm(north)
    east = np.cross(north, up)
    return np.stack([east, north, up])


# --- render ------------------------------------------------------------------------------


def equirect_dirs(width: int, height: int) -> np.ndarray:
    """World (east, north, up) direction of each pixel. Column 0 is north, azimuth grows right."""
    az = (np.arange(width) + 0.5) / width * 2 * math.pi
    alt = math.pi / 2 - (np.arange(height) + 0.5) / height * math.pi
    ca = np.cos(alt)[:, None]
    return np.stack(
        [
            ca * np.sin(az)[None, :],
            ca * np.cos(az)[None, :],
            np.repeat(np.sin(alt)[:, None], width, 1),
        ],
        -1,
    )


def _runs(cols: np.ndarray) -> list[tuple[int, int]]:
    if not len(cols):
        return []
    breaks = np.flatnonzero(np.diff(cols) > 1)
    starts = np.r_[cols[0], cols[breaks + 1]]
    ends = np.r_[cols[breaks], cols[-1]]
    return list(zip(starts.tolist(), (ends + 1).tolist(), strict=True))


def render(
    images: list[np.ndarray],
    rots: list[np.ndarray],
    lens: Lens,
    to_mount: np.ndarray,
    width: int = 4096,
    sharpness: float = 4.0,
    frame_weights: list[float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Blend frames into an equirectangular image. Returns (BGR uint8, coverage mask)."""
    height = width // 2
    acc = np.zeros((height, width, 3), np.float32)
    wsum = np.zeros((height, width), np.float32)
    # A coarse pass finds which part of the panorama each frame can touch.
    cs = 8
    coarse = equirect_dirs(width // cs, height // cs) @ to_mount  # world -> mount
    weights = frame_weights if frame_weights is not None else [1.0] * len(images)
    for img, rot, weight in zip(images, rots, weights, strict=True):
        cam = coarse @ rot  # mount -> camera (rows times rot = rot.T @ v)
        uv, front = project(cam, lens)
        pad = lens.width * 0.05
        hit = front & (uv[..., 0] > -pad) & (uv[..., 0] < lens.width + pad)
        hit &= (uv[..., 1] > -pad) & (uv[..., 1] < lens.height + pad)
        if not hit.any():
            continue
        rows = np.flatnonzero(hit.any(1))
        r0, r1 = max(0, (rows[0] - 1) * cs), min(height, (rows[-1] + 2) * cs)
        for c0, c1 in _runs(np.flatnonzero(hit.any(0))):
            c0, c1 = max(0, (c0 - 1) * cs), min(width, (c1 + 1) * cs)
            sub = _box_dirs(width, height, r0, r1, c0, c1) @ to_mount @ rot
            uv, front = project(sub, lens)
            u, v = uv[..., 0].astype(np.float32), uv[..., 1].astype(np.float32)
            nx = np.minimum(np.abs(u - lens.width / 2) / (lens.width / 2), 1.0)
            ny = np.minimum(np.abs(v - lens.height / 2) / (lens.height / 2), 1.0)
            inside = front & (nx < 1) & (ny < 1)
            if not inside.any():
                continue
            wgt = np.where(inside, ((1 - nx) * (1 - ny)) ** sharpness * weight, 0)
            wgt = wgt.astype(np.float32)
            warped = cv2.remap(img, u, v, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            acc[r0:r1, c0:c1] += warped.astype(np.float32) * wgt[..., None]
            wsum[r0:r1, c0:c1] += wgt
    covered = wsum > 0
    out = np.zeros_like(acc)
    out[covered] = acc[covered] / wsum[covered, None]
    return np.clip(out, 0, 255).astype(np.uint8), covered


def _box_dirs(width, height, r0, r1, c0, c1) -> np.ndarray:
    az = (np.arange(c0, c1) + 0.5) / width * 2 * math.pi
    alt = math.pi / 2 - (np.arange(r0, r1) + 0.5) / height * math.pi
    ca = np.cos(alt)[:, None]
    return np.stack(
        [
            ca * np.sin(az)[None, :],
            ca * np.cos(az)[None, :],
            np.repeat(np.sin(alt)[:, None], len(az), 1),
        ],
        -1,
    )


# --- the whole job -----------------------------------------------------------------------


@dataclass
class StitchResult:
    tiles: int
    used: int
    blank: list[str]
    pairs: int
    matches: int
    median_px: float
    rms_px: float
    focal_px: float
    axis_offset_deg: float
    pitch_scale: float
    level: str
    line_support: float
    compass: bool
    alt_top: float
    alt_bottom: float
    width: int
    height: int


def load_manifest(folder: Path) -> list[Tile]:
    data = json.loads((folder / "manifest.json").read_text())
    return [Tile(t["name"], float(t["pitch"]), float(t["rotation"])) for t in data["tiles"]]


def stitch_folder(
    folder: Path, latitude: float | None = None, width: int = 4096, progress=None
) -> StitchResult:
    """Stitch a scan folder. Writes pano.jpg (equirectangular, cropped to what was seen) and
    pano.json (how the fit went and which rows of the sky the image spans)."""
    say = progress or (lambda msg: None)
    tiles_all = load_manifest(folder)
    images, grays, tiles, blank = [], [], [], []
    for t in tiles_all:
        img = cv2.imread(str(folder / t.name), cv2.IMREAD_COLOR)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Featureless frames (a plain ceiling, or the scope's own base) still fill their spot
        # from the motor angles, but stay out of matching and give way to any other frame.
        if is_blank(gray):
            blank.append(t.name)
        images.append(img)
        grays.append(gray)
        tiles.append(t)
    if not tiles:
        raise ValueError("no usable frames in the scan")
    h, w = grays[0].shape
    lens0 = Lens.for_size(w, h)
    priors = [motor_rotation(t.pitch, t.rotation, lens0) for t in tiles]
    say(f"matching {len(tiles)} frames")
    skip = {k for k, t in enumerate(tiles) if t.name in blank}
    matched = match_pairs(grays, priors, lens0, skip=skip)
    lens0, agree = calibrate_mount(tiles, matched, lens0)
    log.info(
        "pano mount: axis offset %.1f, pitch scale %.2f, %.0f%% of %d pairs agree",
        lens0.axis_offset,
        lens0.pitch_scale,
        100 * agree,
        len(matched),
    )
    pairs = [
        p for p in matched if _pair_disagreement(p, tiles, lens0) <= MAX_PRIOR_DISAGREEMENT_DEG
    ]
    say(f"fitting {len(pairs)} overlaps")
    fit = bundle_adjust(tiles, pairs, lens0)
    log.info(
        "pano fit: %d pairs, %d matches, median %.1f px, f %.0f, axis offset %.1f",
        fit.pairs,
        fit.matches,
        fit.median_px,
        fit.lens.f,
        fit.lens.axis_offset,
    )
    gains = exposure_gains(images, fit.rotations, fit.lens)
    images = [
        np.clip(img * g[None, None, :].astype(np.float32), 0, 255).astype(np.uint8)
        for img, g in zip(images, gains, strict=True)
    ]

    say("finding up")
    normals, weights = line_normals(grays, fit.rotations, fit.lens)
    dirs, lum = _brightness_samples(grays, fit.rotations, fit.lens)
    level = estimate_up(normals, weights, (dirs, lum), latitude)
    frame = world_frame(level)
    # The fitted geometry, so a point in any frame can be put on the sky later.
    fit_out = {
        "lens": asdict(fit.lens),
        "world_rows_enu": frame.tolist(),  # east, north, up as mount-frame vectors
        "tiles": [
            {"name": t.name, "rotation": r.tolist()}  # camera to mount
            for t, r in zip(tiles, fit.rotations, strict=True)
        ],
    }
    (folder / "fit.json").write_text(json.dumps(fit_out))

    say("rendering")
    frame_weights = [BLANK_WEIGHT if k in skip else 1.0 for k in range(len(tiles))]
    pano, covered = render(
        images, fit.rotations, fit.lens, frame, width=width, frame_weights=frame_weights
    )
    rows = np.flatnonzero(covered.any(1))
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    height = pano.shape[0]
    pano = pano[top:bottom]
    cv2.imwrite(str(folder / "pano.tmp.jpg"), pano, [cv2.IMWRITE_JPEG_QUALITY, 88])
    (folder / "pano.tmp.jpg").replace(folder / "pano.jpg")
    result = StitchResult(
        tiles=len(tiles_all),
        used=len(tiles),
        blank=blank,
        pairs=fit.pairs,
        matches=fit.matches,
        median_px=round(fit.median_px, 2),
        rms_px=round(fit.rms_px, 2),
        focal_px=round(fit.lens.f, 1),
        axis_offset_deg=round(fit.lens.axis_offset, 2),
        pitch_scale=round(fit.lens.pitch_scale, 4),
        level=level.method,
        line_support=round(level.support, 3),
        compass=level.north is not None,
        alt_top=round(90 - top / height * 180, 2),
        alt_bottom=round(90 - bottom / height * 180, 2),
        width=int(pano.shape[1]),
        height=int(pano.shape[0]),
    )
    (folder / "pano.json").write_text(json.dumps(asdict(result), indent=1))
    return result


def _brightness_samples(grays, rots, lens, step: int = 40):
    ys, xs = np.mgrid[step // 2 : lens.height : step, step // 2 : lens.width : step]
    uv = np.stack([xs.ravel(), ys.ravel()], -1).astype(float)
    rays = pixel_rays(uv, lens)
    dirs, lum = [], []
    for g, r in zip(grays, rots, strict=True):
        dirs.append(rays @ r.T)
        lum.append(g[ys.ravel(), xs.ravel()].astype(float))
    return np.concatenate(dirs), np.concatenate(lum)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Stitch a wide-camera scan folder into pano.jpg")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--latitude", type=float, default=None)
    ap.add_argument("--width", type=int, default=4096)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    res = stitch_folder(
        args.folder, args.latitude, args.width, progress=lambda m: print(m, flush=True)
    )
    print(json.dumps(asdict(res)))


if __name__ == "__main__":
    main()
