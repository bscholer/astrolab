"""Stitch: frames rendered from a known sphere come back as that sphere.

The frames are drawn with a camera geometry 24 degrees off the default, as on the tripod,
so the stitch has to find it from the frames. The scene's sky is bright and its walls carry
vertical edges, so up is found as on a real scan.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace

import cv2
import numpy as np
import pytest

from server import pano_stitch as ps
from server.scope_pano import plan_stops

W, H = 640, 360
TRUE = replace(ps.Lens.for_size(W, H), axis_offset=171.5, pitch_scale=1.0)


def _scene(seed: int = 3) -> np.ndarray:
    """An equirectangular world: bright sky above, textured walls with vertical edges below."""
    rng = np.random.default_rng(seed)
    w, h = 2048, 1024
    img = np.zeros((h, w, 3), np.uint8)
    img[: h // 2] = (235, 200, 160)  # BGR: pale blue sky
    noise = rng.integers(0, 255, (h // 16, w // 16, 3), dtype=np.uint8)
    img[h // 2 - 120 :] = cv2.resize(noise, (w, h), interpolation=cv2.INTER_NEAREST)[h // 2 - 120 :]
    for _ in range(400):  # blobs and boxes for features
        x, y = int(rng.integers(0, w)), int(rng.integers(h // 2 - 200, h))
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.rectangle(
            img, (x, y), (x + int(rng.integers(5, 40)), y + int(rng.integers(5, 40))), color, -1
        )
    for x in range(0, w, 37):  # posts: vertical edges, as a fence has
        cv2.line(img, (x, h // 2 - 110), (x, h // 2 + 160), (20, 20, 20), 3)
    return img


def _render(scene: np.ndarray, pitch: float, rotation: float) -> np.ndarray:
    ys, xs = np.mgrid[0:H, 0:W].astype(float)
    rays = ps.pixel_rays(np.stack([xs.ravel(), ys.ravel()], -1), TRUE)
    d = rays @ ps.motor_rotation(pitch, rotation, TRUE).T  # mount frame; level, so Z is up
    az = np.arctan2(d[:, 0], d[:, 1]) % (2 * math.pi)
    alt = np.arcsin(np.clip(d[:, 2], -1, 1))
    sh, sw = scene.shape[:2]
    u = (az / (2 * math.pi) * sw).reshape(H, W).astype(np.float32)
    v = ((math.pi / 2 - alt) / math.pi * sh).reshape(H, W).astype(np.float32)
    return cv2.remap(scene, u, v, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


@pytest.fixture(scope="module")
def stitched(tmp_path_factory):
    folder = tmp_path_factory.mktemp("scan")
    scene = _scene()
    stops = [(p, r) for p, r in plan_stops(12.5, 140.0) if 70 <= p <= 145]
    tiles = []
    rng = np.random.default_rng(7)
    for k, (pitch, rot) in enumerate(stops):
        name = f"t{k:03d}.jpg"
        cv2.imwrite(str(folder / name), _render(scene, pitch, rot))
        # The motors are a little off, as real ones are.
        tiles.append(
            {
                "name": name,
                "pitch": pitch + rng.normal(0, 0.3),
                "rotation": rot + rng.normal(0, 0.3),
            }
        )
    (folder / "manifest.json").write_text(json.dumps({"tiles": tiles}))
    result = ps.stitch_folder(folder, latitude=None, width=2048)
    return folder, scene, result


def test_the_mount_geometry_is_found_from_the_frames(stitched):
    _folder, _scene_img, result = stitched
    assert abs(result.axis_offset_deg - TRUE.axis_offset) < 2.0
    assert abs(result.pitch_scale - 1.0) < 0.03
    assert abs(result.focal_px - TRUE.f) / TRUE.f < 0.03
    assert result.median_px < 1.0
    assert not result.compass  # no latitude, no compass


def test_the_panorama_is_level_and_matches_the_scene(stitched):
    folder, scene, result = stitched
    pano = cv2.imread(str(folder / "pano.jpg"))
    assert pano.shape[1] == 2048
    # Put the crop back on the full sphere and compare where the pano saw anything.
    top = round((90 - result.alt_top) / 180 * 1024)
    full = np.zeros_like(scene)
    full[top : top + pano.shape[0]] = pano[: 1024 - top]
    seen = full.max(2) > 0
    seen = cv2.erode(seen.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    assert seen.mean() > 0.1
    # Azimuth zero is arbitrary without a compass: find the turn that fits best.
    gray_s = cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY).astype(float)
    gray_p = cv2.cvtColor(full, cv2.COLOR_BGR2GRAY).astype(float)
    best = max(
        (np.corrcoef(np.roll(gray_s, -shift, 1)[seen], gray_p[seen])[0, 1], shift)
        for shift in range(0, 2048, 8)
    )
    assert best[0] > 0.8
