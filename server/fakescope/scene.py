"""A synthetic backyard: where the scope stands, what blocks the sky, and the weather.

Everything is seeded, so a scene gives the same yard and the same nights every run. The
default yard is loosely modelled on the real one: a house to the north, a big tree to the
southwest, a row of trees along the east fence, and a low fence all round.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class Obstacle:
    """Blocks the sky from az_from to az_to (clockwise, degrees) up to top_alt degrees."""

    name: str
    az_from: float
    az_to: float
    top_alt: float
    kind: str = "tree"  # "tree" has a ragged top, "house" a flat one

    def top(self, az: float) -> float:
        if self.kind != "tree":
            return self.top_alt
        z = math.radians(az)
        return self.top_alt + 4 * math.sin(z * 23) + 2.5 * math.sin(z * 57 + 1.3)

    def covers(self, az: float) -> bool:
        a, b, z = self.az_from % 360, self.az_to % 360, az % 360
        return a <= z <= b if a <= b else z >= a or z <= b


@dataclass(frozen=True)
class Scene:
    lat: float = 44.0153
    lon: float = -121.3462
    elevation_m: float = 1100.0
    fence_alt: float = 8.0
    obstacles: tuple[Obstacle, ...] = (
        Obstacle("house", 300, 60, 48, "house"),
        Obstacle("big tree", 205, 250, 58),
        Obstacle("fence trees", 70, 130, 34),
    )
    seed: int = 1
    # Share of nights by kind. Partly cloudy nights get drifting cloud banks.
    clear_share: float = 0.5
    overcast_share: float = 0.2
    # Frames that fail for no visible reason: a gust, a tracking hiccup.
    random_fail: float = 0.02
    extra: dict = field(default_factory=dict, compare=False)

    def skyline(self, az: float) -> float:
        """The altitude the yard blocks up to at an azimuth."""
        return max([self.fence_alt] + [o.top(az) for o in self.obstacles if o.covers(az)])

    def obstacle_at(self, alt: float, az: float) -> Obstacle | None:
        """The highest obstacle in the way at this spot, or None for the fence or open sky."""
        hits = [o for o in self.obstacles if o.covers(az) and alt < o.top(az)]
        return max(hits, key=lambda o: o.top(az)) if hits else None

    def blocked(self, alt: float, az: float) -> bool:
        return alt < self.skyline(az)

    def _unit(self, *parts: object) -> float:
        """A repeatable number in [0, 1) from the seed and the parts."""
        h = hashlib.sha256(repr((self.seed, *parts)).encode()).digest()
        return int.from_bytes(h[:8], "big") / 2**64

    def night_kind(self, at: datetime) -> str:
        night = (at.astimezone(UTC) + timedelta(hours=self.lon / 15 - 12)).date().isoformat()
        u = self._unit("night", night)
        if u < self.clear_share:
            return "clear"
        if u < self.clear_share + self.overcast_share:
            return "overcast"
        return "partly"

    def cloudy(self, alt: float, az: float, at: datetime) -> bool:
        """Whether cloud covers this spot of sky at this time."""
        kind = self.night_kind(at)
        if kind == "clear":
            return False
        if kind == "overcast":
            return True
        # Cloud banks drifting east at about 15 degrees an hour, gaps between them.
        hours = at.timestamp() / 3600
        x = az + 15 * hours
        wave = math.sin(math.radians(x) * 3 + self._unit("phase") * 6.28)
        wave += 0.6 * math.sin(math.radians(x) * 7 + alt / 20 + self._unit("phase2") * 6.28)
        return wave > 0.4

    def frame_ok(self, alt: float, az: float, at: datetime, index: int) -> bool:
        """Whether a frame at this pointing and time finds stars to register."""
        if alt <= 0 or self.blocked(alt, az) or self.cloudy(alt, az, at):
            return False
        return self._unit("frame", round(at.timestamp()), index) >= self.random_fail
