"""Coverage for the line-streaming subprocess helper.

We test the *parser* without spinning up an actual subprocess. The patterns
we care about are the ones GraXpert and StarNet++ emit; this file pins the
mapping from each pattern to a fraction so a future refactor can't silently
break the live progress bar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from server.subproc import make_line_progress_handler


@dataclass
class _FakeCtx:
    """Minimal RunContext stand-in: collects every progress(fraction, msg)."""

    log: logging.Logger = field(default_factory=lambda: logging.getLogger("t"))
    seen: list[tuple[float, str]] = field(default_factory=list)

    def progress(self, fraction: float, message: str) -> None:
        self.seen.append((fraction, message))


def test_tqdm_percentage_maps_to_band() -> None:
    ctx = _FakeCtx()
    handler = make_line_progress_handler(ctx, low=0.2, high=0.8)
    handler("  42%|███     | 13/32 [00:12<00:18,  1.06it/s]")
    # 42% of [0.2, 0.8] is 0.452.
    assert ctx.seen[-1][0] == _approx(0.452)


def test_explicit_percentage() -> None:
    ctx = _FakeCtx()
    handler = make_line_progress_handler(ctx, low=0.0, high=1.0)
    handler("Progress: 75%")
    assert ctx.seen[-1][0] == _approx(0.75)


def test_starnet_iteration_uses_total_header() -> None:
    """StarNet emits 'Total iterations = N' once, then per-iter lines.
    The handler stitches them together so each iter line maps to k/N."""
    ctx = _FakeCtx()
    handler = make_line_progress_handler(ctx, low=0.0, high=1.0)
    handler("Total iterations = 4")
    handler("Iteration: 1")
    handler("Iteration: 2")
    handler("Iteration: 4")
    fractions = [f for f, _ in ctx.seen]
    # First call advances to 1/4 = 0.25 (the "Total iterations" line is a
    # heartbeat, not an explicit fraction).
    assert _approx(fractions[1]) == 0.25
    assert _approx(fractions[2]) == 0.5
    assert _approx(fractions[3]) == 1.0


def test_progress_never_rewinds() -> None:
    """A late-flushed earlier line must not visibly drag the bar back."""
    ctx = _FakeCtx()
    handler = make_line_progress_handler(ctx, low=0.0, high=1.0)
    handler("90%")
    handler("12%")  # would otherwise overwrite forward progress
    assert ctx.seen[-1][0] >= 0.9


def test_unparseable_line_heartbeats_forward() -> None:
    """When stdout doesn't tell us a percentage, the bar still moves so
    the user sees the process is alive."""
    ctx = _FakeCtx()
    handler = make_line_progress_handler(
        ctx, low=0.1, high=0.9, heartbeat_lines=10
    )
    for i in range(5):
        handler(f"some opaque chatter line {i}")
    # 5 heartbeats of (0.9 - 0.1)/10 = 0.08 each = 0.4 above low (0.1).
    final = ctx.seen[-1][0]
    assert 0.1 < final <= 0.9


def test_blank_lines_ignored() -> None:
    ctx = _FakeCtx()
    handler = make_line_progress_handler(ctx)
    handler("")
    handler("   ")
    assert ctx.seen == []


def _approx(target: float, tol: float = 1e-6) -> float:
    """pytest.approx-lite: lets us put the target on either side of ==."""

    class _A:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, int | float) and abs(other - target) < tol

        def __repr__(self) -> str:
            return f"~{target}"

    return _A()  # type: ignore[return-value]
