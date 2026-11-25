"""Progress monotonicity: ctx.progress fractions must never decrease.

The bar going backwards is the user-facing symptom of two related defects:

1. `make_progress_handler` partitions [low, high] into N equal slices, one per
   Siril sub-command. If N is smaller than the actual number of progress-
   emitting sub-commands, the phase counter saturates at N-1 and every later
   sub-command's fresh 0% sweep recomputes a `scaled` value back inside the
   last slice. The handler used to emit those lower values directly; this
   test pins a monotonic clamp on the handler's emitted fraction so a phase
   miscount surfaces as "bar stalls early" rather than "bar rewinds".

2. `narrowband_extract` and `narrowband_compose` previously declared the
   wrong `phases` count (extract: 4, actual 6; compose HSO: 2, actual 3).
   The regression tests replay a representative Siril progress log against
   each node's stubbed runtime and assert ctx.progress sees a monotonically
   non-decreasing series.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401  ensures node registration runs
from nodes.basic.narrowband_compose import (
    NarrowbandComposeNode,
    NarrowbandComposeParams,
)
from nodes.basic.narrowband_extract import (
    NarrowbandExtractNode,
    NarrowbandExtractParams,
)
from server.models import Ref, RunContext
from server.ports import PortType
from server.siril import SirilBinary, SirilResult, make_progress_handler

# Representative per-sub-command progress trace. Siril emits these for any
# command with a long inner loop; we replay a few percent values from each so
# the handler sees the same shape it would in production.
_SUB_COMMAND_PROGRESS = ("10.00", "50.00", "90.00", "99.00")


def _emit_subcommands(handler, n_sub_commands: int) -> None:
    """Drive the handler with n sequential sub-commands' worth of progress.

    Each sub-command emits the canned `_SUB_COMMAND_PROGRESS` percentages,
    then the next one starts at 10% again. That mid-stream drop is what the
    handler reads as "phase boundary".
    """
    for sub in range(n_sub_commands):
        for pct in _SUB_COMMAND_PROGRESS:
            handler(f"progress: doing thing #{sub}, {pct}%")


# Commands that report progress, and how many 0-100% sweeps each reports.
_SWEEPS = {
    "seqextract_HaOIII": 1, "register": 1, "stack": 2, "link": 1,
    "calibrate": 1, "seqplatesolve": 1, "seqapplyreg": 1,
}


def _emit_script(handler, commands) -> None:
    """Replay what Siril prints for a script, quirks included: a 'Running
    command' line per command, two sweeps for stack (normalization, then
    stacking), and the 'succeeded, 0.00%' line that closes a sequence
    command. That trailing 0% used to be read as the next phase starting."""
    for cmd in commands:
        name = cmd.split()[0]
        handler(f"log: Running command: {name}")
        for sweep in range(_SWEEPS.get(name, 0)):
            for pct in range(0, 101, 5):  # Siril reports per frame; this is coarser
                handler(f"progress: {name} sweep {sweep}, {pct:.2f}%")
        if name in ("seqextract_HaOIII", "register", "calibrate", "seqplatesolve"):
            handler("progress: Sequence processing succeeded., 0.00%")


def _capture_ctx(tmp_path: Path) -> tuple[RunContext, list[float]]:
    """RunContext that records every progress fraction it sees."""
    log: list[float] = []
    ctx = RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: log.append(f),
        log=logging.getLogger("test"),
        cancel=threading.Event(),
    )
    return ctx, log


# ---------------------------------------------------------------------------
# Handler-level: monotonic clamp survives a phases-undercount
# ---------------------------------------------------------------------------


def test_handler_does_not_rewind_when_phases_undercount(
    tmp_path: Path,
) -> None:
    """6 sub-commands replayed through a handler told there are only 4.

    Without the monotonic clamp the bar would rewind every time the phase
    counter saturated and a fresh sub-command opened at 10%. The clamp keeps
    the sequence monotonically non-decreasing; the bar visually stalls near
    `high` instead of going backwards.
    """
    ctx, log = _capture_ctx(tmp_path)
    handler = make_progress_handler(ctx, phases=4)
    _emit_subcommands(handler, n_sub_commands=6)
    assert log, "handler should have emitted at least one progress fraction"
    for prev, curr in zip(log, log[1:], strict=False):
        assert curr >= prev, (
            f"progress went backwards: {prev:.4f} -> {curr:.4f} in {log!r}"
        )


def test_handler_advances_normally_when_phases_match(
    tmp_path: Path,
) -> None:
    """phases=N with exactly N sub-commands: bar advances naturally without
    needing the clamp. Sanity check that the clamp doesn't pin the bar.
    """
    ctx, log = _capture_ctx(tmp_path)
    handler = make_progress_handler(ctx, phases=3)
    _emit_subcommands(handler, n_sub_commands=3)
    assert log[0] < log[-1], (
        f"expected forward motion across 3 phases, got {log!r}"
    )
    for prev, curr in zip(log, log[1:], strict=False):
        assert curr >= prev


# ---------------------------------------------------------------------------
# Node-level: extract + compose drive ctx.progress monotonically
# ---------------------------------------------------------------------------


class ReplayRuntime:
    """SirilRuntime stub that replays a canned Siril progress log into the
    on_log handler, then writes the expected output files so the node's
    post-run guards pass.
    """

    def __init__(
        self,
        *,
        n_sub_commands: int = 0,
        on_complete=None,
        follow_script: bool = False,
    ) -> None:
        self.binary = SirilBinary(path=Path("/fake/siril"), source="env")
        self.n_sub_commands = n_sub_commands
        self.follow_script = follow_script
        self._on_complete = on_complete
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        commands,
        *,
        working_dir=None,
        on_log=None,
        timeout=None,
        require_version="1.4.0",
        cancel=None,
    ) -> SirilResult:
        self.calls.append({"commands": list(commands), "working_dir": working_dir})
        if on_log is not None and self.follow_script:
            _emit_script(on_log, commands)
        elif on_log is not None:
            _emit_subcommands(on_log, self.n_sub_commands)
        if self._on_complete is not None:
            self._on_complete(working_dir)
        return SirilResult(
            returncode=0,
            stdout="ok",
            stderr="",
            ssf="\n".join(commands),
        )


def _make_extract_inputs(tmp_path: Path) -> Path:
    seq_in = tmp_path / "in"
    seq_in.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        (seq_in / f"pp_light_{i:05d}.fit").write_bytes(b"FAKE")
    return seq_in


@pytest.mark.parametrize("shared", [False, True], ids=["separate", "shared"])
def test_narrowband_extract_progress_is_smooth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    """Both registration paths move the bar forward in small steps to the
    end. Before, the node assumed six equal sweeps where Siril prints nine
    (stack reports two, link one): the bar leapt 12% at a time and sat at
    0.95 through the slow final registration.
    """
    seq_in = _make_extract_inputs(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def on_complete(work_dir):
        # The shared path reads the plate-solved debayered sequence and the
        # split frames between its Siril runs.
        (work_dir / "db_pp_light_.seq").write_text(
            "S 'db_pp_light_' 1 3 3 5 -1 4 0 0\nL 3\n"
            + "".join(f"I {i} 1\nR1 0 0 0 1 0 0 0 1 0 0 0 1\n" for i in (1, 2, 3))
        )
        for channel in ("Ha", "OIII"):
            (work_dir / f"{channel}_pp_light_00001.fit").write_bytes(b"X")
        # The node moves work_dir/r_results_{ha,oiii}.fit into out_dir at the
        # end, so the stub needs to place them where the move expects them.
        (work_dir / "r_results_ha.fit").write_bytes(b"HA")
        (work_dir / "r_results_oiii.fit").write_bytes(b"OIII")

    rt = ReplayRuntime(follow_script=True, on_complete=on_complete)
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: rt
    )

    ctx, log = _capture_ctx(tmp_path)
    NarrowbandExtractNode().run(
        inputs={
            "sequence": Ref(
                node_hash="ext",
                port="sequence",
                path=seq_in,
                type=PortType.SEQUENCE_FITS,
            ),
        },
        params=NarrowbandExtractParams(shared_registration=shared),
        ctx=ctx,
        out_dir=out_dir,
    )
    _assert_smooth(log, "narrowband_extract")


def _assert_smooth(log: list[float], node: str) -> None:
    """Never backwards, no big leaps, and the Siril part reaches 0.95."""
    for prev, curr in zip(log, log[1:], strict=False):
        assert curr >= prev, (
            f"{node} progress went backwards: {prev:.4f} -> {curr:.4f}\n"
            f"full series: {log!r}"
        )
    siril = [f for f in log if f < 1.0]
    assert siril[-1] >= 0.94, f"{node} stalled at {siril[-1]:.3f}: {log!r}"
    leaps = [b - a for a, b in zip(siril, siril[1:], strict=False)]
    assert max(leaps) < 0.1, f"{node} jumped {max(leaps):.3f}: {log!r}"


@pytest.mark.parametrize(
    "mode,n_sub_commands",
    [("hoo", 2), ("hso", 3)],
    ids=["non-hso(2 emitters)", "hso(3 emitters)"],
)
def test_narrowband_compose_progress_is_monotonic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    n_sub_commands: int,
) -> None:
    """Compose with HSO emits 3 progress-bearing Siril ops (pm normalize,
    pm synthetic, rgbcomp); non-HSO modes emit 2 (no synthetic step). The
    node picks `phases` to match `mode`, so neither case rewinds.
    """
    ha = tmp_path / "ha.fit"
    oiii = tmp_path / "oiii.fit"
    ha.write_bytes(b"HA")
    oiii.write_bytes(b"OIII")
    out_dir = tmp_path / f"out_{mode}"
    out_dir.mkdir()

    def on_complete(work_dir):
        # Compose writes image.fit into the parent of work_dir (out_dir).
        out_image = work_dir.parent / "image.fit"
        out_image.write_bytes(b"COMPOSED")

    rt = ReplayRuntime(n_sub_commands=n_sub_commands, on_complete=on_complete)
    monkeypatch.setattr(
        "nodes.basic.narrowband_compose.SirilRuntime", lambda *a, **k: rt
    )

    ctx, log = _capture_ctx(tmp_path)
    NarrowbandComposeNode().run(
        inputs={
            "ha": Ref(
                node_hash="ext-ha",
                port="ha",
                path=ha,
                type=PortType.IMAGE_FITS,
            ),
            "oiii": Ref(
                node_hash="ext-oiii",
                port="oiii",
                path=oiii,
                type=PortType.IMAGE_FITS,
            ),
        },
        params=NarrowbandComposeParams(mode=mode),  # type: ignore[arg-type]
        ctx=ctx,
        out_dir=out_dir,
    )
    assert len(log) >= n_sub_commands
    for prev, curr in zip(log, log[1:], strict=False):
        assert curr >= prev, (
            f"narrowband_compose[{mode}] progress went backwards: "
            f"{prev:.4f} -> {curr:.4f}\nfull series: {log!r}"
        )
