"""Instant preview: the finishing steps on a small copy of the image.

The editor's sliders mostly change steps that come after the slow ones
(GraXpert, StarNet, Siril's stretch): color mapping, green removal, the
black point, the stretch, star size and the last touches. Re-running the
pipeline for each change takes seconds to a minute; this runs those steps in
numpy on a copy of the slow steps' output, downscaled to about 1280 px, in a
fraction of a second. The editor commits the change to the real pipeline
once the user stops moving, so the full-size render follows.

How it stays close to the real render:
- the steps are the same maths (server/finish_math.py, tested against
  Siril's output, and the nodes' own numpy code for color mapping);
- statistics (sky, noise, black points) come from a strided sample of the
  full-size image, not from the downscaled copy, whose averaging would
  hide the noise they measure;
- StarNet is replaced by a quick morphological split; star size and
  tightening are therefore approximate.

The base is the output of the node feeding the first finishing step
(graxpert_denoise in the stock templates), taken from the newest version
whose job has it in the cache. Changes to steps above it (crop, gradient
removal, denoise) show once the pipeline has re-rendered.
"""

from __future__ import annotations

import io
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from PIL import Image

from nodes.basic.channel_map import hoo
from nodes.basic.stretch import auto_mtf
from server import finish_math as fm
from server.preview import as_displayed
from server.registry import lookup as registry_lookup

FINISH_KINDS = {
    "channel_map", "color_balance", "auto_bp_shift", "stretch",
    "starnet_extract", "starnet_replace", "starnet_recombine", "save_image",
}
"""Steps the preview can run itself. Everything from the first of these to
the end of the template must be one of them."""

DEFAULT_EDGE = 1280


class NoBase(Exception):
    """No version of this project has the preview's starting image cached yet."""


@dataclass
class Base:
    small: np.ndarray
    """Block-mean downscale, (C, h, w), what the user sees."""
    sample: np.ndarray
    """Every f-th pixel of the full image: noise intact, for statistics."""
    header: fits.Header
    factor: int


def finishing_split(template) -> tuple[int, str, str] | None:
    """(index of the first finishing node, base node id, base port), or None."""
    nodes = template.nodes
    i = len(nodes)
    while i > 0 and nodes[i - 1].kind in FINISH_KINDS:
        i -= 1
    if i >= len(nodes) or i == 0:
        return None
    first = nodes[i]
    src = first.inputs.get("image") if isinstance(first.inputs, dict) else None
    if not src or "." not in src:
        return None
    node_id, port = src.split(".", 1)
    return i, node_id, port


def effective_params(template, overrides: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validated params per finishing node: template values, then overrides."""
    split = finishing_split(template)
    out: dict[str, Any] = {}
    if split is None:
        return out
    for spec in template.nodes[split[0]:]:
        raw = {**(spec.params or {}), **(overrides.get(spec.id) or {})}
        raw = {k: v for k, v in raw.items() if v is not None}
        node_cls = registry_lookup(spec.kind)
        out[spec.id] = node_cls.params_schema(**raw)
    return out


# ---- caches ------------------------------------------------------------------

_lock = threading.Lock()
_bases: OrderedDict[tuple[str, int], Base] = OrderedDict()
_stages: OrderedDict[str, tuple[np.ndarray, np.ndarray]] = OrderedDict()
BASES_KEPT = 3
STAGES_KEPT = 6


def _remember(cache: OrderedDict, key, value, keep: int) -> None:
    with _lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > keep:
            cache.popitem(last=False)


def _recall(cache: OrderedDict, key):
    with _lock:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
        return value


def load_base(path: Path, node_hash: str, max_edge: int) -> Base:
    key = (node_hash, max_edge)
    hit = _recall(_bases, key)
    if hit is not None:
        return hit
    with fits.open(path, memmap=False) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()
    data = np.nan_to_num(data)
    if data.ndim == 2:
        data = data[None]
    h, w = data.shape[1:]
    f = max(1, math.ceil(max(h, w) / max_edge))
    hh, ww = (h // f) * f, (w // f) * f
    small = data[:, :hh, :ww].reshape(data.shape[0], hh // f, f, ww // f, f).mean(axis=(2, 4))
    sample = np.ascontiguousarray(data[:, :hh:f, :ww:f])
    base = Base(small.astype(np.float32), sample, header, f)
    _remember(_bases, key, base, BASES_KEPT)
    return base


# ---- the chain ---------------------------------------------------------------


def _channel_map(arr: np.ndarray, p, scale: int) -> np.ndarray:
    if not p.enabled or arr.shape[0] != 3:
        return arr
    return hoo(arr, p.oiii_strength, p.ha_hue, p.oiii_hue, pixel_scale=scale)


def _color_balance(arr: np.ndarray, p) -> np.ndarray:
    return fm.scnr(arr, 1.0) if p.enabled else arr


def _bp_levels(sample: np.ndarray, p) -> np.ndarray | None:
    """Per-channel black points, measured on the sample as the node does."""
    if not p.enabled:
        return None
    data = sample if sample.shape[0] == 3 else sample[0]
    if data.ndim == 3:
        lit = (data != 0).any(axis=0)
        if not lit.any():
            return None
        bp = float(np.quantile(data[:, lit][:, ::7], p.clip_fraction))
        if p.neutral_sky:
            sky = np.array([fm._sky(data[c], lit) for c in range(3)])
            return np.clip(bp + (sky - sky.mean()), 0.0, 0.999999)
        return np.full(3, min(max(bp, 0.0), 0.999999))
    bp = min(max(float(np.quantile(data, p.clip_fraction)), 0.0), 0.999999)
    return np.array([bp])


def _apply_black(arr: np.ndarray, bps: np.ndarray) -> np.ndarray:
    lit = (arr != 0).any(axis=0)
    out = np.empty_like(arr)
    for c in range(arr.shape[0]):
        b = float(bps[min(c, len(bps) - 1)])
        out[c] = np.clip((arr[c] - b) / (1.0 - b), 0.0, 1.0)
    out[:, ~lit] = 0.0
    return out


def _stretch(small: np.ndarray, sample: np.ndarray, p) -> np.ndarray:
    data = small if small.shape[0] == 3 else small[0]
    sdata = sample if sample.shape[0] == 3 else sample[0]
    if p.method == "autostretch":
        black, mid = fm.autostretch_params(sdata, p.shadows_clip, p.target_bg, p.linked)
        out = fm.apply_mtf(data, black, mid)
    elif p.method == "auto":
        black, mid, _ = auto_mtf(sdata, p.auto_object, p.auto_bg_min, p.auto_bg_max)
        out = fm.apply_mtf(data, black, mid)
    elif p.method == "mtf":
        out = fm.apply_mtf(data, p.mtf_shadows, p.mtf_midtones, p.mtf_highlights)
    else:
        out = fm.asinh_stretch(data, p.asinh_stretch, p.asinh_offset, p.asinh_human)
    out = fm.saturate(out, p.saturation)
    return out if out.ndim == 3 else out[None]


def _key(*parts: Any) -> str:
    def plain(o: Any) -> Any:
        return o.model_dump() if hasattr(o, "model_dump") else str(o)

    return json.dumps(parts, sort_keys=True, default=plain)


def run_chain(base: Base, base_hash: str, template, params: dict[str, Any]) -> np.ndarray:
    """The finishing steps on the base, in template order. Returns (C, h, w) in [0, 1]."""
    split = finishing_split(template)
    assert split is not None
    small, sample = base.small, base.sample
    stretched = False
    key_parts: list[Any] = [base_hash, base.small.shape]
    stars_extract = False
    recombine = None
    for spec in template.nodes[split[0]:]:
        p = params[spec.id]
        kind = spec.kind
        if kind in ("starnet_extract", "starnet_replace", "save_image"):
            if kind == "starnet_extract":
                stars_extract = bool(getattr(p, "enabled", False))
            continue
        if kind == "starnet_recombine":
            recombine = p
            continue
        key_parts.append((spec.id, p))
        key = _key(*key_parts)
        hit = _recall(_stages, key)
        if hit is not None:
            small, sample = hit
            stretched = stretched or kind == "stretch"
            continue
        if kind == "channel_map":
            f = base.factor
            small, sample = _channel_map(small, p, f), _channel_map(sample, p, f)
        elif kind == "color_balance":
            small, sample = _color_balance(small, p), _color_balance(sample, p)
        elif kind == "auto_bp_shift":
            bps = _bp_levels(sample, p)
            if bps is not None:
                small, sample = _apply_black(small, bps), _apply_black(sample, bps)
        elif kind == "stretch":
            small = _stretch(small, sample, p)
            sample = small
            stretched = True
        _remember(_stages, key, (small, sample), STAGES_KEPT)
    out = small if stretched else np.clip(small, 0, 1)
    if recombine is not None and recombine.enabled:
        out = last_touches(out, recombine, stars_extract, base.factor)
    return out


def last_touches(img: np.ndarray, p, stars_split: bool, factor: int) -> np.ndarray:
    """Star size and tightening (on a quick split), then tone and sharpen."""
    if stars_split and (p.stars < 1.0 or p.tighten > 0):
        starless, stars = fm.split_stars(img, size=2.0 / factor)
        stars = fm.tighten_stars(fm.shrink_stars(stars, p.stars), p.tighten, steps=1.0 / factor)
        img = np.clip(starless + stars, 0.0, 1.0)
    img = fm.tone(img, p.contrast, p.highlights, p.shadows)
    img = fm.sharpen(img, p.sharpen, sigma=max(1.5 / factor, 0.8))
    return img


def to_jpeg(img: np.ndarray, header: fits.Header, quality: int = 86) -> bytes:
    arr = np.clip(img, 0.0, 1.0)
    rgb = np.moveaxis(arr, 0, -1) if arr.shape[0] == 3 else np.repeat(arr[0][..., None], 3, axis=2)
    pil = as_displayed(Image.fromarray((rgb * 255 + 0.5).astype(np.uint8), "RGB"), header)
    buf = io.BytesIO()
    pil.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def render(base_path: Path, base_hash: str, template, overrides: dict[str, dict[str, Any]],
           max_edge: int = DEFAULT_EDGE) -> bytes:
    base = load_base(base_path, base_hash, max_edge)
    params = effective_params(template, overrides)
    return to_jpeg(run_chain(base, base_hash, template, params), base.header)
