"""What more data would buy: noise per master, and the gain of more hours.

Noise in a stack falls with the square root of integration time, so from a
master's measured noise and its hours we can say what the next hours are
worth: "about 2 more hours would cut noise by 29%", or "diminishing
returns". The advice shows on the master's page, the last night card and
Tonight, and one click raises the target's goal in the scheduler.

Measurements come from the master's combined linear stack (before any
background extraction or stretch):
- noise: the MAD of differences between neighbouring pixels, which ignores
  gradients and large nebulae that would inflate a plain MAD;
- signal: the 99.5th percentile of an 8x8 block median (stars erased,
  extended light kept) above the sky median.
"""

from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

OPTIONS_H = (1.0, 2.0, 4.0, 8.0)
WORTH_IT = 0.2
"""Two more hours cutting noise by at least this much is worth it."""
SLOWING = 0.1


@dataclass
class Quality:
    sky: float
    noise: float
    signal: float

    @property
    def snr(self) -> float:
        return (self.signal - self.sky) / self.noise if self.noise > 0 else 0.0


_cache: dict[str, Quality] = {}
_lock = threading.Lock()


def measure(path: Path) -> Quality:
    data = fits.getdata(path)
    lum = data.mean(axis=0) if data.ndim == 3 and data.shape[0] in (3, 4) else data
    lum = np.nan_to_num(np.asarray(lum, dtype=np.float32))
    h, w = (lum.shape[0] // 32) * 32, (lum.shape[1] // 32) * 32
    lum = lum[:h, :w]
    nonzero = lum[lum > 0]
    sky = float(np.median(nonzero)) if nonzero.size else 0.0
    # Neighbour differences cancel gradients; MAD/0.6745 is sigma, /sqrt(2)
    # because a difference of two pixels has twice the variance.
    diff = (lum[:, 1:] - lum[:, :-1])[(lum[:, 1:] > 0) & (lum[:, :-1] > 0)]
    noise = 0.0
    if diff.size:
        noise = float(np.median(np.abs(diff - np.median(diff))) / 0.6745 / math.sqrt(2))
    small = lum[::4, ::4]
    sh, sw = (small.shape[0] // 8) * 8, (small.shape[1] // 8) * 8
    blocks = small[:sh, :sw].reshape(sh // 8, 8, sw // 8, 8).swapaxes(1, 2)
    blocks = blocks.reshape(sh // 8, sw // 8, 64)
    signal = float(np.percentile(np.median(blocks, axis=2), 99.5)) if blocks.size else sky
    return Quality(sky, noise, signal)


def measure_cached(node_hash: str, path: Path) -> Quality | None:
    with _lock:
        if node_hash in _cache:
            return _cache[node_hash]
    if not path.exists():
        return None
    q = measure(path)
    with _lock:
        _cache[node_hash] = q
    return q


def noise_drop(hours_s: float, more_s: float) -> float:
    """Fraction by which noise falls when `more_s` is added to `hours_s`."""
    if hours_s <= 0:
        return 1.0
    return 1 - math.sqrt(hours_s / (hours_s + more_s))


@dataclass
class Advice:
    hours: float
    snr: float | None
    options: list[dict[str, float]]
    verdict: str
    """worth_it, slowing, or diminishing."""
    headline: str
    third_h: float
    """Hours more to cut noise by a third."""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt_h(h: float) -> str:
    if h < 1:
        return f"{round(h * 60)} minutes"
    if h < 10:
        n = f"{h:.1f}".removesuffix(".0")
        return f"{n} hour{'' if n == '1' else 's'}"
    return f"{round(h)} hours"


def advise(total_s: float, quality: Quality | None) -> Advice:
    hours = total_s / 3600
    snr = quality.snr if quality else None
    options = []
    for more in OPTIONS_H:
        drop = noise_drop(total_s, more * 3600)
        options.append({
            "hours": more,
            "noise_drop": drop,
            "snr": snr / (1 - drop) if snr is not None else None,
        })
    two = noise_drop(total_s, 2 * 3600)
    third_h = 1.25 * hours  # sqrt(T / (T + x)) = 2/3  ->  x = 1.25 T
    if two >= WORTH_IT:
        verdict = "worth_it"
        headline = f"About 2 more hours would cut noise by {round(two * 100)}%"
    elif two >= SLOWING:
        verdict = "slowing"
        headline = f"{_fmt_h(third_h)} more would cut noise by a third"
    else:
        verdict = "diminishing"
        ten = noise_drop(total_s, 10 * 3600)
        headline = f"Diminishing returns: 10 more hours for {round(ten * 100)}%"
    return Advice(hours, snr, options, verdict, headline, third_h)


SCOPE_FILTERS = {"HAOIII": "duo", "DUO": "duo", "ASTRO": "astro", "VIS": "vis"}


def scope_filter(filter_name: str | None) -> str | None:
    key = (filter_name or "").replace("-", "").replace(" ", "").upper()
    return SCOPE_FILTERS.get(key)


def queue_more(
    targets: list[dict[str, Any]],
    integrated_s: dict[str, float],
    *,
    name: str,
    more_h: float,
    ra_deg: float | None,
    dec_deg: float | None,
    object_type: str | None,
    filter_name: str | None,
    exposure_s: int,
    common_name: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Raise `name`'s goal in the scheduler's target list by `more_h`, adding it if missing.

    The goal counts hours the scheduler has captured for the target, so the
    new goal is what it has shot so far plus `more_h`.
    """
    shot_h = integrated_s.get(name, 0.0) / 3600
    out = [dict(t) for t in targets]
    for t in out:
        if t.get("name") == name:
            t["goal_hours"] = round(max(t.get("goal_hours") or 0.0, shot_h) + more_h, 2)
            return out, t
    if ra_deg is None or dec_deg is None:
        raise ValueError(f"no sky position known for {name}")
    t = {
        "name": name,
        "ra_deg": ra_deg,
        "dec_deg": dec_deg,
        "object_type": object_type,
        "filter": scope_filter(filter_name),
        "exposure_s": exposure_s if exposure_s in (15, 30, 60) else 30,
        "gain": None,
        "goal_hours": round(shot_h + more_h, 2),
        "display_name": name,
        "common_name": common_name,
    }
    out.append(t)
    return out, t
