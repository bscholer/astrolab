"""Stream a subprocess's stdout/stderr line by line, with cancel + progress.

The Siril runner already does this for .ssf scripts; this is the same idea
for nodes that wrap a generic CLI binary (GraXpert, StarNet++, …) that the
user otherwise stares at while it sits at 10% for ten minutes. Both AI tools
have under-documented stdout formats, so the helper takes a lenient parser
that maps any line we recognize to a fraction and otherwise just logs it.

If we never extract a fraction, we still nudge the progress bar forward as
each line of output arrives so the UI shows the process is alive — far
better than a frozen indicator.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("astrolab.subproc")


# --- progress patterns -------------------------------------------------------
# Several CLI tools we wrap (GraXpert, StarNet++) print progress in one of:
#   - tqdm-style "  42%|████   | 13/32 [00:12<00:18,  1.06it/s]"
#   - explicit percentage  "Progress: 42.5%"
#   - X-of-N progress      "Tile 7 of 16"
#   - "Iteration: K"  (StarNet, with a prior "Total iterations = N" line)
#
# We try them in order; the first pattern that matches wins. Anything else
# is treated as a heartbeat line so the bar still advances.

_TQDM_PCT_RE = re.compile(r"\b(\d{1,3})\s*%\s*\|")
_PCT_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*%")
_X_OF_N_RE = re.compile(r"\b(\d+)\s*(?:of|/)\s*(\d+)\b", re.IGNORECASE)
_TOTAL_ITER_RE = re.compile(r"total\s+iterations?\s*[=:]\s*(\d+)", re.IGNORECASE)
_ITER_RE = re.compile(r"\biteration\s*[:#]?\s*(\d+)", re.IGNORECASE)


@dataclass
class _ProgressState:
    """Mutable state threaded through the line callback."""

    last_frac: float = 0.0
    """Highest fraction we've reported so the bar never visibly rewinds."""

    total_iters: int | None = None
    """Captured from a 'Total iterations = N' header so subsequent
    'Iteration: K' lines map to K/N. None until we see the header."""

    heartbeat_step: float = 0.0
    """Counts unparseable output lines so the bar still creeps forward."""


def make_line_progress_handler(
    ctx,
    *,
    low: float = 0.1,
    high: float = 0.95,
    prefix: str = "",
    heartbeat_lines: int = 60,
) -> Callable[[str], None]:
    """Build a per-line callback that forwards parsed progress to ctx.progress.

    `low`/`high`: the band of the overall progress bar this subprocess
    occupies. The node still owns the [0, low) pre-flight and (high, 1.0]
    cleanup fractions outside this helper.

    `heartbeat_lines`: when we can't parse a fraction from stdout, advance
    the bar by one Nth of (high - low) per output line up to a cap of 80%
    of the band. 60 lines means a typical multi-minute AI run will visibly
    creep without overshooting if we never see an explicit percentage.
    """

    state = _ProgressState()
    band = high - low
    heartbeat_cap = low + band * 0.8

    def report(frac: float, msg: str) -> None:
        # Clamp + monotonic: progress never appears to go backward, since
        # downstream consumers rely on the bar being a reasonable proxy.
        scaled = max(low, min(high, low + max(0.0, min(1.0, frac)) * band))
        if scaled < state.last_frac:
            scaled = state.last_frac
        else:
            state.last_frac = scaled
        ctx.progress(scaled, f"{prefix}{msg}" if prefix else msg)

    def heartbeat(msg: str) -> None:
        # Move forward by 1/heartbeat_lines of the band per line, capped so
        # we don't accidentally hit `high` before the subprocess actually
        # finishes. Once at the cap we stop advancing but still ship the
        # message so the UI's status text stays alive.
        step = band / max(heartbeat_lines, 1)
        nudge = min(state.last_frac + step, heartbeat_cap)
        if nudge > state.last_frac:
            state.last_frac = nudge
            ctx.progress(state.last_frac, f"{prefix}{msg}" if prefix else msg)
        else:
            # Bar is parked at the cap; refresh just the message.
            ctx.progress(state.last_frac, f"{prefix}{msg}" if prefix else msg)

    def handler(line: str) -> None:
        ctx.log.debug("subproc: %s", line)
        if not line.strip():
            return

        # Capture 'Total iterations = N' so a later 'Iteration: K' has a
        # denominator. StarNet++ emits the total once, then per-iter lines.
        m_total = _TOTAL_ITER_RE.search(line)
        if m_total:
            try:
                state.total_iters = int(m_total.group(1))
            except ValueError:
                pass

        m_tqdm = _TQDM_PCT_RE.search(line)
        if m_tqdm:
            try:
                pct = float(m_tqdm.group(1)) / 100.0
                report(pct, line.strip())
                return
            except ValueError:
                pass

        m_pct = _PCT_RE.search(line)
        if m_pct:
            try:
                pct = float(m_pct.group(1)) / 100.0
                if 0.0 <= pct <= 1.0:
                    report(pct, line.strip())
                    return
            except ValueError:
                pass

        m_iter = _ITER_RE.search(line)
        if m_iter and state.total_iters:
            try:
                k = int(m_iter.group(1))
                report(k / max(state.total_iters, 1), line.strip())
                return
            except ValueError:
                pass

        m_xn = _X_OF_N_RE.search(line)
        if m_xn:
            try:
                k = int(m_xn.group(1))
                n = int(m_xn.group(2))
                if n > 0:
                    report(k / n, line.strip())
                    return
            except ValueError:
                pass

        # No structured progress; nudge the bar via heartbeat so the user
        # sees activity instead of a frozen 10%.
        heartbeat(line.strip())

    return handler


# --- subprocess runner -------------------------------------------------------


@dataclass
class StreamedResult:
    returncode: int
    stdout: str
    """Captured stdout (and merged stderr). Useful for error messages on
    non-zero exits; on success callers usually rely on the side effects of
    the subprocess and ignore this."""


def run_streamed(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    timeout: float | None = None,
) -> StreamedResult:
    """Run `argv` and stream every output line through `on_line`.

    stdout and stderr are merged so progress lines that happen to come from
    stderr (tqdm, GraXpert phase headers) reach the same parser. The full
    captured stream is returned in StreamedResult so callers can include
    its tail in error messages.

    `cancel`: cooperative cancel token. When set, we terminate the
    subprocess and raise JobCancelled — same semantics as SirilRuntime.
    """
    from .runtime import JobCancelled  # local: avoid module-load cycle

    if cancel is not None and cancel.is_set():
        raise JobCancelled()

    proc = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered: progress lines flush as they're written
    )

    watchdog_stop = threading.Event()
    watchdog: threading.Thread | None = None

    if cancel is not None:
        def _watch() -> None:
            while not watchdog_stop.is_set():
                if cancel.wait(timeout=0.5):
                    log.info("subproc: cancel set, terminating pid %d", proc.pid)
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                    return

        watchdog = threading.Thread(target=_watch, daemon=True)
        watchdog.start()

    chunks: list[str] = []
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            chunks.append(line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # pragma: no cover  (defensive)
                    log.exception("on_line callback raised; continuing")
        proc.wait(timeout=timeout)

        if cancel is not None and cancel.is_set():
            raise JobCancelled()

        return StreamedResult(returncode=proc.returncode, stdout="\n".join(chunks))
    finally:
        watchdog_stop.set()
        if watchdog is not None:
            watchdog.join(timeout=1.0)
