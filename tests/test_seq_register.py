"""seq_register node: unit tests with stubbed SirilRuntime.

The platesolve path makes TWO runtime.run() calls (one per Siril process):
  call[0]: seqplatesolve  -- validated by .seq R1 lines + success marker in stdout
  call[1]: seqapplyreg    -- validated by output frame files

The star path makes ONE runtime.run() call (register + seqapplyreg in one process).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401
from nodes.basic.seq_register import SeqRegisterNode, SeqRegisterParams, _seq_has_registration
from server.models import Ref, RunContext
from server.ports import PortType
from server.siril import SirilBinary, SirilResult

# Minimal .seq content with R1 registration lines, as written by seqplatesolve.
_SEQ_WITH_REG = (
    "#Siril sequence file.\n"
    "S 'bkg_pp_light_' 1 3 3 5 1 6 0 0 0\n"
    "L 3\n"
    "I 1 1\n"
    "I 2 1\n"
    "I 3 1\n"
    "R1 8.3 8.3 0.8 0 0.007 224 H 1.0 0.003 -14.6 0.003 -1.0 2161.0 1e-09 -1e-09 1\n"
    "R1 8.2 8.2 0.8 0 0.007 234 H 1.0 0.003 -15.5 0.003 -1.0 2162.0 2e-09 -1e-09 1\n"
    "R1 8.4 8.4 0.8 0 0.007 227 H 1.0 0.003 -19.6 0.003 -1.0 2163.0 2e-09 -1e-09 1\n"
)

_PS_SUCCESS_STDOUT = (
    "log: Astrometric registration computed.\n"
    "Writing sequence file bkg_pp_light_.seq\n"
)

_APPLY_SUCCESS_STDOUT = (
    "progress: Sequence processing succeeded., 0.00%\n"
    "log: Sequence processing succeeded.\n"
)


class FakeRuntime:
    """Fake SirilRuntime that records calls and returns configurable results.

    For platesolve tests, set up two separate result objects via `results`:
    results[0] = seqplatesolve response, results[1] = seqapplyreg response.
    The on_run callback is called for every invocation; use it to create files.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "ok",
        on_run: Any | None = None,
        results: list[SirilResult] | None = None,
    ) -> None:
        self.binary = SirilBinary(path=Path("/fake/siril"), source="env")
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[dict[str, Any]] = []
        self._on_run = on_run
        self._results = results  # per-call overrides; None = use returncode/stdout

    def run(self, commands, *, working_dir=None, on_log=None, timeout=None,
            require_version="1.4.0", cancel=None) -> SirilResult:
        idx = len(self.calls)
        self.calls.append(
            {"commands": list(commands), "working_dir": working_dir, "require": require_version}
        )
        if self._on_run is not None:
            self._on_run(idx, commands, working_dir)
        if self._results is not None and idx < len(self._results):
            return self._results[idx]
        return SirilResult(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr="",
            ssf="\n".join(commands),
        )


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: None,
        log=logging.getLogger("test"),
    )


def _make_seq_dir(root: Path, basename: str, n_frames: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(1, n_frames + 1):
        (root / f"{basename}_{i:05d}.fit").write_bytes(b"FAKE")
    return root


def _platesolve_on_run(seq_out: Path, basename: str, out_basename: str, fitseq: bool):
    """Return an on_run callback suitable for platesolve two-process tests."""
    def on_run(idx: int, cmds, wd):
        if idx == 0:
            # seqplatesolve: write .seq with R1 registration lines
            (seq_out / f"{basename}_.seq").write_text(_SEQ_WITH_REG)
        elif idx == 1:
            # seqapplyreg: write output frames
            if fitseq:
                (seq_out / f"{out_basename}.fit").write_bytes(b"FAKE")
            else:
                for i in (1, 2, 3):
                    (seq_out / f"{out_basename}_{i:05d}.fit").write_bytes(b"FAKE")
    return on_run


def test_platesolve_default_emits_seqplatesolve_and_seqapplyreg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default platesolve mode runs TWO Siril processes: seqplatesolve then seqapplyreg."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "bkg_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    results = [
        SirilResult(returncode=0, stdout=_PS_SUCCESS_STDOUT, stderr="", ssf="ps"),
        SirilResult(returncode=0, stdout=_APPLY_SUCCESS_STDOUT, stderr="", ssf="sar"),
    ]
    fake_rt = FakeRuntime(
        results=results,
        on_run=_platesolve_on_run(seq_out, "bkg_pp_light", "r_bkg_pp_light", fitseq=True),
    )
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)

    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )

    # Two separate Siril invocations.
    assert len(fake_rt.calls) == 2
    ps_cmds = fake_rt.calls[0]["commands"]
    sar_cmds = fake_rt.calls[1]["commands"]

    # First call: seqplatesolve, no seqapplyreg.
    assert any(c.startswith("seqplatesolve bkg_pp_light") for c in ps_cmds)
    assert not any(c.startswith("seqapplyreg ") for c in ps_cmds)
    assert not any(c.startswith("register ") for c in ps_cmds)

    # Second call: seqapplyreg only, no seqplatesolve.
    apply_cmd = next(c for c in sar_cmds if c.startswith("seqapplyreg "))
    assert apply_cmd.startswith("seqapplyreg bkg_pp_light")
    assert "-framing=max" in apply_cmd
    assert "-kernel=square" in apply_cmd
    assert not any(c.startswith("seqplatesolve ") for c in sar_cmds)

    # seqplatesolve flags on the first call.
    ps_cmd = next(c for c in ps_cmds if c.startswith("seqplatesolve "))
    assert "-nocache" in ps_cmd
    assert "-force" in ps_cmd
    # distortion is OFF by default (Siril 1.4.2 crashes on finalize with -disto).
    assert "-disto=ps_distortion" not in ps_cmd


def test_distortion_true_emits_disto_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """distortion=True (opt-in) adds -disto=ps_distortion to seqplatesolve."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "bkg_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    results = [
        SirilResult(returncode=0, stdout=_PS_SUCCESS_STDOUT, stderr="", ssf="ps"),
        SirilResult(returncode=0, stdout=_APPLY_SUCCESS_STDOUT, stderr="", ssf="sar"),
    ]
    fake_rt = FakeRuntime(
        results=results,
        on_run=_platesolve_on_run(seq_out, "bkg_pp_light", "r_bkg_pp_light", fitseq=True),
    )
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)

    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(distortion=True),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    ps_cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("seqplatesolve "))
    assert "-disto=ps_distortion" in ps_cmd


def test_platesolve_crash_tolerated_when_seq_has_reg_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seqplatesolve exit -11 (finalize crash) is tolerated when reg data is in the .seq."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "bkg_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    results = [
        # seqplatesolve crashes (exit -11) but writes the success marker + .seq
        SirilResult(returncode=-11, stdout=_PS_SUCCESS_STDOUT, stderr="", ssf="ps"),
        SirilResult(returncode=0, stdout=_APPLY_SUCCESS_STDOUT, stderr="", ssf="sar"),
    ]
    fake_rt = FakeRuntime(
        results=results,
        on_run=_platesolve_on_run(seq_out, "bkg_pp_light", "r_bkg_pp_light", fitseq=True),
    )
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)

    # Must NOT raise even though seqplatesolve exited -11.
    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert len(fake_rt.calls) == 2


def test_platesolve_hard_fail_when_no_success_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seqplatesolve failure without the success marker always raises."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "bkg_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    results = [
        # Crash with "Finalizing sequence processing failed." but no success marker.
        SirilResult(
            returncode=-11,
            stdout="log: Finalizing sequence processing failed.\n",
            stderr="",
            ssf="ps",
        ),
        SirilResult(returncode=0, stdout=_APPLY_SUCCESS_STDOUT, stderr="", ssf="sar"),
    ]

    def on_run(idx, cmds, wd):
        if idx == 0:
            # .seq written but WITHOUT R1 registration lines (partial failure)
            (seq_out / "bkg_pp_light_.seq").write_text(
                "#Siril sequence file.\nS 'bkg_pp_light_' 1 3 3 5 1 6 0 0 0\nI 1 1\n"
            )

    fake_rt = FakeRuntime(results=results, on_run=on_run)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)

    with pytest.raises(RuntimeError, match="seqplatesolve exited -11"):
        SeqRegisterNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                                type=PortType.SEQUENCE_FITS),
            },
            params=SeqRegisterParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_star_method_emits_register_two_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Star method uses a single Siril process (register + seqapplyreg)."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def on_run(idx, cmds, wd):
        (out_dir / "sequence" / "r_pp_light.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(stdout=_APPLY_SUCCESS_STDOUT, on_run=on_run)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)

    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(method="star", input_basename="pp_light"),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    # Single Siril process for star method.
    assert len(fake_rt.calls) == 1
    cmds = fake_rt.calls[0]["commands"]
    reg = next(c for c in cmds if c.startswith("register "))
    assert reg.startswith("register pp_light ")
    assert "-2pass" in reg
    assert "-transf=homography" in reg
    assert not any(c.startswith("seqplatesolve ") for c in cmds)


def test_filter_options_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "bkg_pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    results = [
        SirilResult(returncode=0, stdout=_PS_SUCCESS_STDOUT, stderr="", ssf="ps"),
        SirilResult(returncode=0, stdout=_APPLY_SUCCESS_STDOUT, stderr="", ssf="sar"),
    ]
    fake_rt = FakeRuntime(
        results=results,
        on_run=_platesolve_on_run(seq_out, "bkg_pp_light", "r_bkg_pp_light", fitseq=True),
    )
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(filter_fwhm=0.2, filter_round=0.1),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    # Filter flags are in the seqapplyreg call (second invocation).
    apply_cmd = next(c for c in fake_rt.calls[1]["commands"] if c.startswith("seqapplyreg "))
    assert "-filter-fwhm=0.2" in apply_cmd
    assert "-filter-round=0.1" in apply_cmd


def test_per_frame_no_count_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seqapplyreg can drop frames; we don't enforce input==output count."""
    seq_in = _make_seq_dir(tmp_path / "in", "bkg_pp_light", n_frames=5)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    def on_run(idx, cmds, wd):
        seq_out.mkdir(exist_ok=True)
        if idx == 0:
            (seq_out / "bkg_pp_light_.seq").write_text(_SEQ_WITH_REG)
        elif idx == 1:
            for i in (1, 2, 3, 4):  # 4 of 5 inputs make it through
                (seq_out / f"r_bkg_pp_light_{i:05d}.fit").write_bytes(b"")

    results = [
        SirilResult(returncode=0, stdout=_PS_SUCCESS_STDOUT, stderr="", ssf="ps"),
        SirilResult(returncode=0, stdout=_APPLY_SUCCESS_STDOUT, stderr="", ssf="sar"),
    ]
    fake_rt = FakeRuntime(results=results, on_run=on_run)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    refs = SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(fitseq=False),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    frames = sorted((refs["sequence"].path).glob("r_bkg_pp_light_*.fit"))
    assert len(frames) == 4


def test_drizzle_default_off_emits_no_drizzle_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default params (drizzle=False) must not emit any drizzle-related flag."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "bkg_pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    results = [
        SirilResult(returncode=0, stdout=_PS_SUCCESS_STDOUT, stderr="", ssf="ps"),
        SirilResult(returncode=0, stdout=_APPLY_SUCCESS_STDOUT, stderr="", ssf="sar"),
    ]
    fake_rt = FakeRuntime(
        results=results,
        on_run=_platesolve_on_run(seq_out, "bkg_pp_light", "r_bkg_pp_light", fitseq=True),
    )
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    apply_cmd = next(c for c in fake_rt.calls[1]["commands"] if c.startswith("seqapplyreg "))
    assert "-drizzle" not in apply_cmd
    assert "-pixfrac" not in apply_cmd
    assert "-scale=" not in apply_cmd


def test_drizzle_on_emits_drizzle_pixfrac_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """drizzle=True attaches -drizzle, -scale, and -pixfrac to seqapplyreg."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "bkg_pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    results = [
        SirilResult(returncode=0, stdout=_PS_SUCCESS_STDOUT, stderr="", ssf="ps"),
        SirilResult(returncode=0, stdout=_APPLY_SUCCESS_STDOUT, stderr="", ssf="sar"),
    ]
    fake_rt = FakeRuntime(
        results=results,
        on_run=_platesolve_on_run(seq_out, "bkg_pp_light", "r_bkg_pp_light", fitseq=True),
    )
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(drizzle=True),  # default scale=2, pixfrac=0.7
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    apply_cmd = next(c for c in fake_rt.calls[1]["commands"] if c.startswith("seqapplyreg "))
    assert "-drizzle" in apply_cmd
    assert "-scale=2" in apply_cmd
    assert "-pixfrac=0.7" in apply_cmd


def test_raises_when_seqapplyreg_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real seqapplyreg failure (non-zero exit, no success marker) raises RuntimeError."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "bkg_pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seq_out = out_dir / "sequence"

    results = [
        SirilResult(returncode=0, stdout=_PS_SUCCESS_STDOUT, stderr="", ssf="ps"),
        SirilResult(returncode=3, stdout="something went wrong", stderr="", ssf="sar"),
    ]

    def on_run(idx, cmds, wd):
        seq_out.mkdir(exist_ok=True)
        if idx == 0:
            (seq_out / "bkg_pp_light_.seq").write_text(_SEQ_WITH_REG)

    fake_rt = FakeRuntime(results=results, on_run=on_run)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="exited 3"):
        SeqRegisterNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                                type=PortType.SEQUENCE_FITS),
            },
            params=SeqRegisterParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_seq_has_registration_true_with_r1_lines(tmp_path: Path) -> None:
    """_seq_has_registration returns True when .seq contains R1 lines."""
    seq_file = tmp_path / "test.seq"
    seq_file.write_text(_SEQ_WITH_REG)
    assert _seq_has_registration(seq_file) is True


def test_seq_has_registration_false_without_r1_lines(tmp_path: Path) -> None:
    """_seq_has_registration returns False for a .seq with only I lines (pre-platesolve)."""
    seq_file = tmp_path / "test.seq"
    seq_file.write_text(
        "#Siril sequence file.\n"
        "S 'bkg_pp_light_' 1 3 3 5 1 6 0 0 0\n"
        "L 3\n"
        "I 1 1\n"
        "I 2 1\n"
        "I 3 1\n"
    )
    assert _seq_has_registration(seq_file) is False


def test_seq_has_registration_false_for_missing_file(tmp_path: Path) -> None:
    """_seq_has_registration returns False when the file doesn't exist."""
    assert _seq_has_registration(tmp_path / "nonexistent.seq") is False
