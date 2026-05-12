"""seq_stack node: unit tests with stubbed SirilRuntime."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import nodes.basic  # noqa: F401
from nodes.basic.seq_stack import SeqStackNode, SeqStackParams
from server.models import Ref, RunContext
from server.ports import PortType
from server.siril import SirilBinary, SirilResult

_SUCCESS_STDOUT = (
    "progress: Sequence processing succeeded., 0.00%\n"
    "log: Sequence processing succeeded.\n"
)
_STACK_SUCCESS_STDOUT = "log: Stacked sequence successfully.\n"


class FakeRuntime:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "ok",
        on_run: Any | None = None,
    ) -> None:
        self.binary = SirilBinary(path=Path("/fake/siril"), source="env")
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[dict[str, Any]] = []
        self._on_run = on_run

    def run(self, commands, *, working_dir=None, on_log=None, timeout=None,
            require_version="1.4.0", cancel=None) -> SirilResult:
        self.calls.append(
            {"commands": list(commands), "working_dir": working_dir, "require": require_version}
        )
        if self._on_run is not None:
            self._on_run(commands, working_dir)
        return SirilResult(
            returncode=self.returncode, stdout=self.stdout, stderr="", ssf="\n".join(commands),
        )


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: None,
        log=logging.getLogger("test"),
    )


def test_stack_default_rej_writes_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.fit").write_bytes(b"STACKED")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)
    refs = SeqStackNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqStackParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    img = refs["image"]
    assert img.type is PortType.IMAGE_FITS
    assert img.path == out_dir / "image.fit"
    assert img.path.exists()

    stack_cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("stack "))
    assert stack_cmd.startswith("stack r_pp_light rej w 3.0 3.0 ")
    assert "-norm=addscale" in stack_cmd
    assert "-output_norm" in stack_cmd
    assert "-32bits" not in stack_cmd  # 1.4 default
    assert f"-out={out_dir.resolve() / 'image.fit'}" in stack_cmd


def test_stack_mean_no_rejection_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        # Non-empty payload; _check_siril_stack_result rejects zero-byte
        # output as "missing or empty".
        (out_dir / "image.fit").write_bytes(b"STACKED")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)
    SeqStackNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqStackParams(method="mean"),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    stack_cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("stack "))
    assert "stack r_pp_light mean" in stack_cmd
    assert " w " not in stack_cmd  # no rejection type
    assert "3.0 3.0" not in stack_cmd


def test_stack_raises_when_image_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="missing"):
        SeqStackNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                                type=PortType.SEQUENCE_FITS),
            },
            params=SeqStackParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_stack_raises_when_siril_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=4)
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="exited 4"):
        SeqStackNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                                type=PortType.SEQUENCE_FITS),
            },
            params=SeqStackParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


# ---------------------------------------------------------------------------
# OOM fallback: winsor -> percentile retry
# ---------------------------------------------------------------------------


def _seq_ref(path: Path) -> Ref:
    return Ref(node_hash="ext", port="sequence", path=path, type=PortType.SEQUENCE_FITS)


def test_winsor_succeeds_no_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Winsor stack succeeds -> no retry, one runtime.run() call."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.fit").write_bytes(b"STACKED")

    fake_rt = FakeRuntime(returncode=0, on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)

    refs = SeqStackNode().run(
        inputs={"sequence": _seq_ref(seq_in)},
        params=SeqStackParams(rejection_type="w"),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert refs["image"].path.exists()
    # Only one run() call — no retry.
    assert len(fake_rt.calls) == 1
    stack_cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("stack "))
    assert " w " in stack_cmd


def test_winsor_oom_fallback_to_percentile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Winsor fails with no output -> retry fires with percentile, output is the retry result."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    call_count = 0

    def fake(cmds, wd):
        nonlocal call_count
        call_count += 1
        # First call (winsor): no output, no success marker -> OOM signature.
        # Second call (percentile fallback): write the output and switch the
        # FakeRuntime stdout to include the stack success marker so the
        # post-retry _check_siril_stack_result accepts the shutdown segfault.
        if call_count == 2:
            (out_dir / "image.fit").write_bytes(b"STACKED_PERCENTILE")
            fake_rt.stdout = _STACK_SUCCESS_STDOUT

    fake_rt = FakeRuntime(returncode=-11, stdout="crash", on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)

    with caplog.at_level(logging.WARNING, logger="nodes.basic.seq_stack"):
        refs = SeqStackNode().run(
            inputs={"sequence": _seq_ref(seq_in)},
            params=SeqStackParams(rejection_type="w"),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )

    assert refs["image"].path.exists()
    assert len(fake_rt.calls) == 2
    # Retry command uses percentile.
    retry_cmd = next(c for c in fake_rt.calls[1]["commands"] if c.startswith("stack "))
    assert " p " in retry_cmd
    assert "0.1 0.1" in retry_cmd
    assert any("winsor" in r.message for r in caplog.records)
    assert any("percentile" in r.message for r in caplog.records)


def test_winsor_oom_percentile_also_fails_raises_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Winsor fails AND percentile retry also fails -> original winsor error is raised."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    # Both calls fail with rc=-11 and no output written.
    fake_rt = FakeRuntime(returncode=-11, stdout="crash")
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)

    with pytest.raises(RuntimeError, match="exited -11"):
        SeqStackNode().run(
            inputs={"sequence": _seq_ref(seq_in)},
            params=SeqStackParams(rejection_type="w"),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )

    # Both calls were attempted.
    assert len(fake_rt.calls) == 2


def test_percentile_fails_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User-configured percentile rejection fails -> no retry (already lowest-memory option)."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=-11, stdout="crash")
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)

    with pytest.raises(RuntimeError, match="exited -11"):
        SeqStackNode().run(
            inputs={"sequence": _seq_ref(seq_in)},
            params=SeqStackParams(rejection_type="p", sigma_low=0.1, sigma_high=0.1),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )

    # Only one run() call — no fallback attempted for percentile.
    assert len(fake_rt.calls) == 1


# ---------------------------------------------------------------------------
# Schema: sigma_low / sigma_high constraints
# ---------------------------------------------------------------------------


def test_sigma_low_accepts_percentile_fraction() -> None:
    """sigma_low=0.1 (percentile fraction) must now be accepted."""
    p = SeqStackParams(sigma_low=0.1, sigma_high=0.1, rejection_type="p")
    assert p.sigma_low == pytest.approx(0.1)
    assert p.sigma_high == pytest.approx(0.1)


def test_sigma_low_zero_still_rejected() -> None:
    """sigma_low=0 is still invalid (gt=0.0)."""
    with pytest.raises(ValidationError):
        SeqStackParams(sigma_low=0.0)


def test_sigma_high_above_max_rejected() -> None:
    """sigma_high=11 exceeds le=10.0 and must be rejected."""
    with pytest.raises(ValidationError):
        SeqStackParams(sigma_high=11.0)
