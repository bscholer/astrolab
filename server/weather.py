"""Short-range forecast from Open-Meteo (free, no key) and a rain warning for the scope."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&hourly=cloud_cover,precipitation,snowfall,precipitation_probability"
    "&forecast_days=2&timezone=UTC"
)
SPRINKLE_MM_PER_HOUR = 0.5
TOTAL_MM = 2.0


@dataclass(frozen=True)
class Outlook:
    max_cloud_pct: float
    mean_cloud_pct: float
    precip_mm: float
    snow_cm: float
    warn: bool
    reason: str | None


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def outlook(
    lat: float, lon: float, now_iso: str, hours: int = 12, fetch: Callable[[str], dict] = _fetch
) -> Outlook:
    """Summarise the next `hours` hours. Light drizzle is fine. Snow or real rain warns."""
    h = fetch(URL.format(lat=lat, lon=lon))["hourly"]
    start = next((i for i, t in enumerate(h["time"]) if t >= now_iso[:13]), 0)
    sl = slice(start, start + hours)
    cloud = [c or 0 for c in h["cloud_cover"][sl]]
    rain = [p or 0 for p in h["precipitation"][sl]]
    snow = [s or 0 for s in h["snowfall"][sl]]
    total, snowfall = sum(rain), sum(snow)
    reason = None
    if snowfall > 0:
        reason = f"snow forecast ({snowfall:.1f} cm)"
    elif max(rain, default=0) >= SPRINKLE_MM_PER_HOUR or total >= TOTAL_MM:
        reason = f"rain forecast ({total:.1f} mm)"
    return Outlook(
        max_cloud_pct=max(cloud, default=0),
        mean_cloud_pct=round(sum(cloud) / len(cloud), 1) if cloud else 0,
        precip_mm=round(total, 2),
        snow_cm=round(snowfall, 2),
        warn=reason is not None,
        reason=reason,
    )
