"""Find the sky in the stitched panorama, take hand fixes, and trace the horizon from it.

The panorama is equirectangular: column 0 is azimuth 0 and azimuth grows to the right; row 0
is altitude alt_top and rows step down by 180 / (2 * width) degrees. With a compass (the
mount is polar aligned) those are true altitude and azimuth. The masks cover the whole
sphere (row 0 is altitude 90), so hand fixes stay put when a restitch crops differently.

The sky test is the one first written for single wide frames: smooth, bright or blue areas
that connect to a large patch of sure sky. It suits a daytime scan; at night there is little
to go on, and the hand fixes do the work.

Files in the scan folder:
  sky_auto.png   what the test found: 255 sky, 0 not sky, 128 not seen
  sky_edits.png  hand fixes: 0 none, 1 sky, 2 not sky
  horizon.json   altitude of the top of whatever blocks the sky, per degree of azimuth
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

MASK_WIDTH = 2048  # 5.7 px per degree, close to what the frame test was tuned on (7)
SKY, GROUND, UNSEEN = 255, 0, 128
EDIT_NONE, EDIT_SKY, EDIT_GROUND = 0, 1, 2

SEED_MIN_FRACTION = 1500 / (320 * 180)  # of the image, as on a single frame
TEXTURE_SEED = 5.0
TEXTURE_GROW = 14.0
BLUE_MARGIN = 8.0
BRIGHT_SKY = 175.0
THIN_DEG = 1.0  # obstacles thinner than this (wires, string lights) do not count
GROUND_BELOW_DEG = -5.0  # not-sky this far below the horizon is the ground itself


def _box_mean(a: np.ndarray, k: int) -> np.ndarray:
    return cv2.blur(a, (k, k), borderType=cv2.BORDER_REPLICATE)


def _close(mask: np.ndarray, radius: int) -> np.ndarray:
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    return cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, k) > 0


def sky_mask(rgb: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    """True where a pixel is sky. rgb is (h, w, 3) RGB; valid marks pixels that were seen."""
    a = rgb.astype(np.float32)
    gray = a.mean(2)
    mean = _box_mean(gray, 7)
    texture = np.sqrt(np.maximum(_box_mean(gray * gray, 7) - mean**2, 0))
    blue = a[..., 2] - a[..., 0]
    seen = np.ones(gray.shape, bool) if valid is None else valid
    seed = (
        seen
        & (texture < TEXTURE_SEED)
        & (gray > 105)
        & ((blue > BLUE_MARGIN) | (gray > BRIGHT_SKY))
    )
    n, labels, stats, _ = cv2.connectedComponentsWithStats(seed.astype(np.uint8), connectivity=4)
    min_px = SEED_MIN_FRACTION * gray.size
    ok = np.zeros(n, bool)
    flat_blue, flat_gray = blue.ravel(), gray.ravel()
    flat_labels = labels.ravel()
    sums_blue = np.bincount(flat_labels, flat_blue, n)
    sums_gray = np.bincount(flat_labels, flat_gray, n)
    sizes = stats[:, cv2.CC_STAT_AREA]
    for k in range(1, n):
        if sizes[k] < min_px:
            continue
        if sums_blue[k] / sizes[k] > BLUE_MARGIN or sums_gray[k] / sizes[k] > BRIGHT_SKY:
            ok[k] = True
    seed_ok = ok[labels]
    grow = seen & (gray > 100) & (texture < TEXTURE_GROW) & (blue > 0)
    candidate = _close(grow | seed_ok, 2) & seen
    n2, labels2 = cv2.connectedComponents(candidate.astype(np.uint8), connectivity=4)
    touched = np.zeros(n2, bool)
    touched[np.unique(labels2[seed_ok & candidate])] = True
    touched[0] = False
    return _close(touched[labels2], 3) & seen


MASK_HEIGHT = MASK_WIDTH // 2


def detect(folder: Path, alt_top: float) -> np.ndarray:
    """Run the sky test on the panorama and save sky_auto.png. Returns the auto mask."""
    pano = cv2.imread(str(folder / "pano.jpg"), cv2.IMREAD_COLOR)
    if pano is None:
        raise FileNotFoundError("no panorama in the scan")
    h = round(pano.shape[0] * MASK_WIDTH / pano.shape[1])
    small = cv2.resize(pano, (MASK_WIDTH, h), interpolation=cv2.INTER_AREA)
    # The stitcher leaves unseen pixels black.
    seen = small.max(2) > 0
    seen = cv2.erode(seen.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    sky = sky_mask(cv2.cvtColor(small, cv2.COLOR_BGR2RGB), seen)
    part = np.full(sky.shape, UNSEEN, np.uint8)
    part[seen] = GROUND
    part[sky] = SKY
    auto = np.full((MASK_HEIGHT, MASK_WIDTH), UNSEEN, np.uint8)
    top = int(round((90.0 - alt_top) / 180.0 * MASK_HEIGHT))
    rows = min(h, MASK_HEIGHT - top)
    auto[top : top + rows] = part[:rows]
    tmp = folder / "sky_auto.tmp.png"
    cv2.imwrite(str(tmp), auto)
    tmp.replace(folder / "sky_auto.png")
    return auto


def load_auto(folder: Path) -> np.ndarray:
    path = folder / "sky_auto.png"
    auto = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE) if path.is_file() else None
    if auto is None:
        raise FileNotFoundError("the sky has not been found in this scan yet")
    return auto


def load_edits(folder: Path, shape: tuple[int, int]) -> np.ndarray:
    path = folder / "sky_edits.png"
    edits = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE) if path.is_file() else None
    if edits is None or edits.shape != shape:
        return np.zeros(shape, np.uint8)
    return edits


def save_edits(folder: Path, edits: np.ndarray) -> None:
    edits = np.where(np.isin(edits, (EDIT_SKY, EDIT_GROUND)), edits, EDIT_NONE).astype(np.uint8)
    tmp = folder / "sky_edits.tmp.png"
    cv2.imwrite(str(tmp), edits)
    tmp.replace(folder / "sky_edits.png")


def final_mask(folder: Path) -> np.ndarray:
    """The auto mask with the hand fixes laid over it."""
    auto = load_auto(folder)
    edits = load_edits(folder, auto.shape)
    out = auto.copy()
    out[edits == EDIT_SKY] = SKY
    out[edits == EDIT_GROUND] = GROUND
    return out


def horizon_profile(
    mask: np.ndarray,
    alt_top: float = 90.0,
    step_deg: float = 1.0,
    keep: np.ndarray | None = None,
) -> list[float | None]:
    """Altitude of the top of whatever blocks the sky, for each step of azimuth.

    None where the scan did not reach. Where everything seen is sky, the lowest altitude
    seen: the view is open at least down to there.

    Only not-sky that stands on the ground counts: trees have trunks, the house has walls.
    A patch floating in the sky (a cloud the test missed, a dark frame near the Sun) does
    not. keep marks not-sky that counts anyway: what was painted by hand.
    """
    h, w = mask.shape
    deg_per_px = 360.0 / w
    alts = alt_top - (np.arange(h) + 0.5) * deg_per_px
    ground = mask == GROUND
    thin = max(1, round(THIN_DEG / deg_per_px))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (thin, thin))
    ground = cv2.morphologyEx(ground.astype(np.uint8), cv2.MORPH_OPEN, k) > 0
    n, labels = cv2.connectedComponents(ground.astype(np.uint8), connectivity=8)
    standing = np.zeros(n, bool)
    low = alts < GROUND_BELOW_DEG
    standing[np.unique(labels[low][ground[low]])] = True
    # The panorama wraps at north: a region cut there is one region.
    for a, b in ((0, -1), (-1, 0)):
        standing[np.unique(labels[:, a][standing[labels[:, b]]])] = True
    standing[0] = False
    ground = standing[labels]
    if keep is not None:
        ground |= keep
    seen = mask != UNSEEN
    out: list[float | None] = []
    bins = int(round(360 / step_deg))
    for b in range(bins):
        c0 = int(b * w / bins)
        c1 = max(c0 + 1, int((b + 1) * w / bins))
        tops = []
        for c in range(c0, c1):
            col_seen = np.flatnonzero(seen[:, c])
            if not len(col_seen):
                continue
            col_ground = np.flatnonzero(ground[:, c])
            tops.append(alts[col_ground[0]] if len(col_ground) else alts[col_seen[-1]])
        out.append(round(float(np.percentile(tops, 80)), 2) if tops else None)
    return out


def save_horizon(folder: Path, result: dict) -> dict:
    """Trace the horizon from the final mask and write horizon.json."""
    mask = final_mask(folder)
    painted = load_edits(folder, mask.shape) == EDIT_GROUND
    profile = horizon_profile(mask, keep=painted)
    data = {
        "step_deg": 1.0,
        "alt": profile,
        "compass": bool(result.get("compass")),
        "scan": folder.name,
    }
    (folder / "horizon.json").write_text(json.dumps(data))
    return data


def load_horizon(folder: Path) -> dict | None:
    path = folder / "horizon.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
