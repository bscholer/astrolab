"""auto_bp_shift node: BP computation + Siril command emission."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits

import nodes.basic  # noqa: F401  registers
from nodes.basic.auto_bp_shift import AutoBpShiftNode, AutoBpShiftParams, _compute_bp
from server.models import Ref, RunContext
from server.ports import PortType
from server.siril import SirilBinary, SirilResult


class FakeRuntime:
    def __init__(self, *, returncode: int = 0, on_run: Any | None = None) -> None:
        self.binary = SirilBinary(path=Path("/fake/siril"), source="env")
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []
        self._on_run = on_run

    def run(self, commands, *, working_dir=None, on_log=None, timeout=None,
            require_version="1.4.0", cancel=None) -> SirilResult:
        self.calls.append({"commands": list(commands), "working_dir": working_dir})
        if self._on_run is not None:
            self._on_run(commands, working_dir)
        return SirilResult(
            returncode=self.returncode, stdout="ok", stderr="", ssf="\n".join(commands),
        )


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: None,
        log=logging.getLogger("test"),
    )


def _write_fits(path: Path, data: np.ndarray) -> Path:
    fits.PrimaryHDU(data.astype(np.float32)).writeto(path, overwrite=True)
    return path


# ---- _compute_bp -------------------------------------------------------------


def test_compute_bp_returns_quantile_for_uniform_data(tmp_path: Path) -> None:
    """At 1% clip, BP should land at ~the 1st percentile of a uniform [0,1]
    distribution: ~0.01."""
    rng = np.random.default_rng(seed=42)
    data = rng.uniform(0.0, 1.0, size=(512, 512))
    src = _write_fits(tmp_path / "uniform.fit", data)
    bp = _compute_bp(src, 0.01)
    # Tolerance: 512x512 = 262144 samples; the 1st-percentile estimator
    # is well within 0.005 of 0.01 at that sample size.
    assert 0.005 < bp < 0.015


def test_compute_bp_clip_fraction_zero_returns_min(tmp_path: Path) -> None:
    """clip_fraction=0 means 'don't clip anything' — BP should be the
    floor of the data, clamped to >= 0."""
    data = np.array([[0.1, 0.2, 0.3, 0.4]], dtype=np.float32)
    src = _write_fits(tmp_path / "small.fit", data)
    bp = _compute_bp(src, 0.0)
    assert bp == pytest.approx(0.1, abs=1e-5)


def test_compute_bp_clamps_negative_to_zero(tmp_path: Path) -> None:
    """Post-bg-extract data can have slightly negative pixels. The BP
    must clamp to [0, 1) so linstretch doesn't reject the command."""
    data = np.array([[-0.05, -0.01, 0.1, 0.5]], dtype=np.float32)
    src = _write_fits(tmp_path / "neg.fit", data)
    bp = _compute_bp(src, 0.01)
    assert bp == 0.0


def test_compute_bp_renormalizes_uint16_range(tmp_path: Path) -> None:
    """A uint16-scaled FITS (values up to 65535) must be renormalized to
    [0, 1] before handing the BP value to linstretch — otherwise BP comes
    out as e.g. 655 and Siril rejects it."""
    rng = np.random.default_rng(seed=1)
    data = rng.uniform(0.0, 65535.0, size=(256, 256)).astype(np.float32)
    src = _write_fits(tmp_path / "u16.fit", data)
    bp = _compute_bp(src, 0.01)
    # 1st-percentile of [0, 65535] uniform is ~655, divided by 65535 ~= 0.01
    assert 0.005 < bp < 0.02


# ---- Node end-to-end (mocked Siril) ------------------------------------------


def test_run_emits_linstretch_with_computed_bp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = np.random.default_rng(seed=7)
    data = rng.uniform(0.0, 1.0, size=(256, 256))
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        # Mimic siril writing the saved FITS.
        (out_dir / "image.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.auto_bp_shift.SirilRuntime", lambda *a, **k: fake_rt
    )

    refs = AutoBpShiftNode().run(
        inputs={
            "image": Ref(node_hash="ext", port="image", path=src,
                         type=PortType.IMAGE_FITS),
        },
        params=AutoBpShiftParams(clip_fraction=0.01),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )

    linstretch_cmd = next(
        c for c in fake_rt.calls[0]["commands"] if c.startswith("linstretch")
    )
    assert "-clipmode=clip" in linstretch_cmd
    assert "-BP=" in linstretch_cmd
    bp_str = linstretch_cmd.split("-BP=")[1].split()[0]
    bp = float(bp_str)
    # 1% clip on uniform [0,1] should land near 0.01.
    assert 0.005 < bp < 0.02
    assert refs["image"].path.exists()


def test_run_raises_when_siril_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = np.linspace(0.0, 1.0, 1024, dtype=np.float32).reshape(32, 32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=3)
    monkeypatch.setattr(
        "nodes.basic.auto_bp_shift.SirilRuntime", lambda *a, **k: fake_rt
    )

    with pytest.raises(RuntimeError, match="exited 3"):
        AutoBpShiftNode().run(
            inputs={
                "image": Ref(node_hash="ext", port="image", path=src,
                             type=PortType.IMAGE_FITS),
            },
            params=AutoBpShiftParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_run_passthrough_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enabled=False must skip Siril entirely and copy the input to the
    output port. Siril should not be invoked."""
    data = np.array([[0.2, 0.3, 0.4]], dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.auto_bp_shift.SirilRuntime", lambda *a, **k: fake_rt
    )
    refs = AutoBpShiftNode().run(
        inputs={
            "image": Ref(node_hash="ext", port="image", path=src,
                         type=PortType.IMAGE_FITS),
        },
        params=AutoBpShiftParams(enabled=False),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert fake_rt.calls == []  # Siril never ran
    assert refs["image"].path.exists()
    # Passthrough preserves the exact input bytes; reload and compare.
    with fits.open(refs["image"].path) as hdul:
        out_data = np.asarray(hdul[0].data)
    np.testing.assert_array_equal(out_data, data)


def test_run_raises_when_output_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Siril returned 0 but didn't write the saved FITS — surface the
    failure with the stdout tail rather than handing back a stale Ref."""
    data = np.full((16, 16), 0.5, dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()  # no on_run -> no output file written
    monkeypatch.setattr(
        "nodes.basic.auto_bp_shift.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="missing"):
        AutoBpShiftNode().run(
            inputs={
                "image": Ref(node_hash="ext", port="image", path=src,
                             type=PortType.IMAGE_FITS),
            },
            params=AutoBpShiftParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


# ---- Siril 1.4 shutdown-segfault tolerance -----------------------------------


def test_rc_minus11_output_present_warns_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """rc=-11 (Siril 1.4 shutdown segfault) + output written -> success with warning."""
    data = np.full((16, 16), 0.5, dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(returncode=-11, on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.auto_bp_shift.SirilRuntime", lambda *a, **k: fake_rt
    )

    with caplog.at_level(logging.WARNING, logger="nodes._seq_runner"):
        refs = AutoBpShiftNode().run(
            inputs={
                "image": Ref(node_hash="ext", port="image", path=src,
                             type=PortType.IMAGE_FITS),
            },
            params=AutoBpShiftParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )

    assert refs["image"].path.exists()
    assert any("segfault" in r.message for r in caplog.records)
    assert any("-11" in r.message for r in caplog.records)


def test_rc_minus11_output_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=-11 + output absent -> failure (real crash, not just shutdown segfault)."""
    data = np.full((16, 16), 0.5, dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=-11)
    monkeypatch.setattr(
        "nodes.basic.auto_bp_shift.SirilRuntime", lambda *a, **k: fake_rt
    )

    with pytest.raises(RuntimeError, match="missing or empty"):
        AutoBpShiftNode().run(
            inputs={
                "image": Ref(node_hash="ext", port="image", path=src,
                             type=PortType.IMAGE_FITS),
            },
            params=AutoBpShiftParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_rc0_output_present_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=0 + output written -> success (baseline happy path)."""
    data = np.full((16, 16), 0.5, dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(returncode=0, on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.auto_bp_shift.SirilRuntime", lambda *a, **k: fake_rt
    )
    refs = AutoBpShiftNode().run(
        inputs={
            "image": Ref(node_hash="ext", port="image", path=src,
                         type=PortType.IMAGE_FITS),
        },
        params=AutoBpShiftParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert refs["image"].path.exists()
