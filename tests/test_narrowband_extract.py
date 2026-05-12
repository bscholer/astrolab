"""narrowband_extract node: unit tests with a stubbed SirilRuntime.

Covers the Siril command list it emits (the expensive Phase 1 of the
narrowband flow). The compose-side tests live in
test_narrowband_compose.py; integration of the two on a real CFA
sequence happens on the Linux box.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401  ensures the node registers
from nodes.basic.narrowband_extract import (
    NarrowbandExtractNode,
    NarrowbandExtractParams,
)
from server.models import Ref, RunContext
from server.ports import PortType
from server.siril import SirilBinary, SirilResult


class FakeRuntime:
    def __init__(self, *, returncode: int = 0, on_run: Any | None = None) -> None:
        self.binary = SirilBinary(path=Path("/fake/siril"), source="env")
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []
        self._on_run = on_run

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
        self.calls.append(
            {"commands": list(commands), "working_dir": working_dir, "require": require_version}
        )
        if self._on_run is not None:
            self._on_run(commands, working_dir)
        return SirilResult(
            returncode=self.returncode,
            stdout="ok",
            stderr="",
            ssf="\n".join(commands),
        )


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: None,
        log=logging.getLogger("test"),
        cancel=threading.Event(),
    )


def _make_seq_dir(root: Path, basename: str, n_frames: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(1, n_frames + 1):
        (root / f"{basename}_{i:05d}.fit").write_bytes(b"FAKE")
    return root


def _run_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_outputs: bool = True,
) -> tuple[FakeRuntime, Path]:
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        if write_outputs:
            # The node moves work_dir/r_results_{ha,oiii}.fit to out_dir at the
            # end, so write them where the node expects to find them.
            work_dir = out_dir / "_narrowband"
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "r_results_ha.fit").write_bytes(b"HA")
            (work_dir / "r_results_oiii.fit").write_bytes(b"OIII")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )
    NarrowbandExtractNode().run(
        inputs={
            "sequence": Ref(
                node_hash="ext",
                port="sequence",
                path=seq_in,
                type=PortType.SEQUENCE_FITS,
            ),
        },
        params=NarrowbandExtractParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    return fake_rt, out_dir


def test_seqextract_runs_first_with_resample_ha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-resample=ha is load-bearing: without it Ha comes out at half-rez."""
    rt, _ = _run_node(tmp_path, monkeypatch)
    cmds = rt.calls[0]["commands"]
    extract = next(c for c in cmds if c.startswith("seqextract_HaOIII "))
    assert extract == "seqextract_HaOIII pp_light -resample=ha"
    extract_idx = cmds.index(extract)
    register_idx = next(
        i for i, c in enumerate(cmds) if c.startswith("register Ha_pp_light")
    )
    assert extract_idx < register_idx


def test_per_channel_register_and_stack_are_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register Ha -> stack Ha -> register OIII -> stack OIII. Order matters
    so the downstream `link results` finds both stacks in place."""
    rt, _ = _run_node(tmp_path, monkeypatch)
    cmds = rt.calls[0]["commands"]
    reg_ha = cmds.index("register Ha_pp_light")
    stack_ha = next(
        i for i, c in enumerate(cmds) if c.startswith("stack r_Ha_pp_light rej 3 3")
    )
    reg_oiii = cmds.index("register OIII_pp_light")
    stack_oiii = next(
        i for i, c in enumerate(cmds) if c.startswith("stack r_OIII_pp_light rej 3 3")
    )
    assert reg_ha < stack_ha < reg_oiii < stack_oiii
    for stack_cmd in (cmds[stack_ha], cmds[stack_oiii]):
        assert "-norm=addscale" in stack_cmd
        assert "-output_norm" in stack_cmd
        assert "-32b" in stack_cmd
    assert "-out=results_ha" in cmds[stack_ha]
    assert "-out=results_oiii" in cmds[stack_oiii]


def test_results_seq_is_materialized_before_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare stacks need to be copied under basename_NNNNN.fit naming and
    linked into results.seq before `register results` will work. Regression
    for the bug fixed in PR #48 — must not silently regress here either."""
    rt, _ = _run_node(tmp_path, monkeypatch)
    cmds = rt.calls[0]["commands"]
    align_idx = cmds.index("register results -transf=shift -interp=none")

    pre = cmds[:align_idx]
    expected_pre = [
        "load results_ha",
        "save results_00001",
        "load results_oiii",
        "save results_00002",
        "link results",
    ]
    indices = [pre.index(line) for line in expected_pre]
    assert indices == sorted(indices), (
        "expected results.seq setup to appear in order before register, "
        f"got these indices in {pre!r}: {indices}"
    )

    post = cmds[align_idx + 1 :]
    expected_post = [
        "load r_results_00001",
        "save r_results_ha",
        "load r_results_00002",
        "save r_results_oiii",
    ]
    indices = [post.index(line) for line in expected_post]
    assert indices == sorted(indices)


def test_outputs_returned_with_correct_ports_and_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        work_dir = out_dir / "_narrowband"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "r_results_ha.fit").write_bytes(b"HA")
        (work_dir / "r_results_oiii.fit").write_bytes(b"OIII")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )
    refs = NarrowbandExtractNode().run(
        inputs={
            "sequence": Ref(
                node_hash="ext",
                port="sequence",
                path=seq_in,
                type=PortType.SEQUENCE_FITS,
            ),
        },
        params=NarrowbandExtractParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert set(refs.keys()) == {"ha", "oiii"}
    assert refs["ha"].port == "ha"
    assert refs["oiii"].port == "oiii"
    assert refs["ha"].type == PortType.IMAGE_FITS
    assert refs["oiii"].type == PortType.IMAGE_FITS
    assert refs["ha"].path == out_dir / "r_results_ha.fit"
    assert refs["oiii"].path == out_dir / "r_results_oiii.fit"
    assert refs["ha"].path.exists()
    assert refs["oiii"].path.exists()
    # Working dir should have been cleaned up after the move.
    assert not (out_dir / "_narrowband").exists()


def test_missing_input_dir_raises_before_siril(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        NarrowbandExtractNode().run(
            inputs={
                "sequence": Ref(
                    node_hash="ext",
                    port="sequence",
                    path=tmp_path / "missing",
                    type=PortType.SEQUENCE_FITS,
                ),
            },
            params=NarrowbandExtractParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
    assert fake_rt.calls == []


def test_empty_input_sequence_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="no input frames"):
        NarrowbandExtractNode().run(
            inputs={
                "sequence": Ref(
                    node_hash="ext",
                    port="sequence",
                    path=seq_in,
                    type=PortType.SEQUENCE_FITS,
                ),
            },
            params=NarrowbandExtractParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
    assert fake_rt.calls == []


def test_raises_when_siril_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=5)
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="exited 5"):
        NarrowbandExtractNode().run(
            inputs={
                "sequence": Ref(
                    node_hash="ext",
                    port="sequence",
                    path=seq_in,
                    type=PortType.SEQUENCE_FITS,
                ),
            },
            params=NarrowbandExtractParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_raises_when_outputs_missing_after_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean exit + missing aligned stacks must trip the post-run guard."""
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()  # no on_run, so the expected files never land
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="missing"):
        NarrowbandExtractNode().run(
            inputs={
                "sequence": Ref(
                    node_hash="ext",
                    port="sequence",
                    path=seq_in,
                    type=PortType.SEQUENCE_FITS,
                ),
            },
            params=NarrowbandExtractParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


# ---- Siril 1.4 shutdown-segfault tolerance -----------------------------------


def _make_seq_input(tmp_path: Path) -> Ref:
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light")
    return Ref(node_hash="ext", port="sequence", path=seq_in, type=PortType.SEQUENCE_FITS)


def test_rc0_both_outputs_present_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=0 + both outputs written -> success (baseline happy path)."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        work_dir = out_dir / "_narrowband"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "r_results_ha.fit").write_bytes(b"HA")
        (work_dir / "r_results_oiii.fit").write_bytes(b"OIII")

    fake_rt = FakeRuntime(returncode=0, on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )
    refs = NarrowbandExtractNode().run(
        inputs={"sequence": _make_seq_input(tmp_path)},
        params=NarrowbandExtractParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert refs["ha"].path.exists()
    assert refs["oiii"].path.exists()


def test_rc_minus11_both_outputs_present_warns_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """rc=-11 (Siril 1.4 shutdown segfault) + both outputs present -> success with warning."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        work_dir = out_dir / "_narrowband"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "r_results_ha.fit").write_bytes(b"HA")
        (work_dir / "r_results_oiii.fit").write_bytes(b"OIII")

    fake_rt = FakeRuntime(returncode=-11, on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )

    with caplog.at_level(logging.WARNING, logger="nodes._seq_runner"):
        refs = NarrowbandExtractNode().run(
            inputs={"sequence": _make_seq_input(tmp_path)},
            params=NarrowbandExtractParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )

    assert refs["ha"].path.exists()
    assert refs["oiii"].path.exists()
    assert any("segfault" in r.message for r in caplog.records)
    assert any("-11" in r.message for r in caplog.records)


def test_rc_minus11_outputs_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=-11 + outputs absent -> failure (real crash, not just shutdown segfault)."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=-11)
    monkeypatch.setattr(
        "nodes.basic.narrowband_extract.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="missing or empty"):
        NarrowbandExtractNode().run(
            inputs={"sequence": _make_seq_input(tmp_path)},
            params=NarrowbandExtractParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
