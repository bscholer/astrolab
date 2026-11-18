"""Plan a whole night two ways, and write the same night out for Bend.

The scheduler picks greedily: the best target for the next minute. This looks at the
whole night (half-hour slots) and finds the best total, so you can see how much the
greedy choice leaves behind. `uv run python -m experiments.bend_planner.night`
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from astropy.time import Time

from server import scope_plan, sky
from server.scope_plan import Candidate, hour_gain

SLOT_MIN = 30
HERE = Path(__file__).parent


def demo_night(n_targets: int = 4, n_slots: int = 8):
    """Targets spread east to west of the meridian at 22:00 local, so some are setting."""
    loc = sky.observer_location(37.0, -122.0, 100.0)
    start = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    lst = Time(start, scale="utc").sidereal_time("apparent", longitude=loc.lon).deg
    targets = [
        Candidate(f"t{i}", float((lst + (i - n_targets / 2) * 25) % 360), 30.0 + 8 * (i % 3))
        for i in range(n_targets)
    ]
    return loc, start, targets, n_slots


def alt_table(loc, start, targets, n_slots) -> list[list[float]]:
    """sin^2(altitude) per slot and target, 0 where the planner would not use it."""
    rows = []
    for s in range(n_slots):
        at = start + timedelta(minutes=SLOT_MIN * s)
        row = []
        for c in targets:
            alt = scope_plan.usable(c, loc, at)
            row.append(0.0 if alt is None else math.sin(math.radians(alt)) ** 2)
        rows.append(row)
    return rows


def gain_table(n_slots: int) -> list[float]:
    return [hour_gain(k * SLOT_MIN * 60) for k in range(n_slots + 1)]


def greedy(alt: list[list[float]], gain: list[float]) -> float:
    counts = [0] * len(alt[0])
    total = 0.0
    for row in alt:
        scores = [gain[counts[t]] * a for t, a in enumerate(row)]
        t = max(range(len(row)), key=scores.__getitem__)
        if scores[t] > 0:
            total += scores[t]
            counts[t] += 1
    return total


def best_plan(alt: list[list[float]], gain: list[float]) -> float:
    """Every assignment of targets to slots (or idle), same scoring as greedy."""

    def go(s: int, counts: tuple[int, ...]) -> float:
        if s == len(alt):
            return 0.0
        top = go(s + 1, counts)
        for t, a in enumerate(alt[s]):
            if a > 0:
                nxt = counts[:t] + (counts[t] + 1,) + counts[t + 1:]
                top = max(top, gain[counts[t]] * a + go(s + 1, nxt))
        return top

    return go(0, (0,) * len(alt[0]))


def emit_bend(alt, gain) -> str:
    rows = ", ".join("[" + ", ".join(f"{v:.5f}" for v in r) + "]" for r in alt)
    g = ", ".join(f"{v:.5f}" for v in gain)
    return (
        (HERE / "planner.bend").read_text()
        + f"\ndef alt_table():\n  return [{rows}]\n"
        + f"\ndef gain_table():\n  return [{g}]\n"
        + f"\ndef main():\n  return best(0, {len(alt)}, {len(alt[0])}, zeros({len(alt[0])}))\n"
    )


def main() -> None:
    loc, start, targets, n_slots = demo_night()
    alt = alt_table(loc, start, targets, n_slots)
    gain = gain_table(n_slots)
    g, b = greedy(alt, gain), best_plan(alt, gain)
    print(f"greedy {g:.5f}   whole-night best {b:.5f}   ({(b / g - 1) * 100:+.1f}%)")
    if "--emit-bend" in sys.argv:
        out = HERE / "night.bend"
        out.write_text(emit_bend(alt, gain))
        print(f"wrote {out}; run: bend run-c {out}  (expect {b:.4f})")


if __name__ == "__main__":
    main()
