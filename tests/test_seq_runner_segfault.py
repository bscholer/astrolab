"""Tests for Siril 1.4 shutdown-segfault tolerance in _seq_runner.

Siril 1.4.x sometimes exits -11 (SIGSEGV) during process teardown after a
sequence operation succeeds. _check_siril_seq_result and run_siril_on_sequence
must accept that pattern when outputs are present, and still reject real
failures.

All tests mock the subprocess; no real Siril binary is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from nodes._seq_runner import (
    _check_siril_seq_result,
    _check_siril_stack_result,
    run_siril_on_sequence,
)
from server.models import RunContext
from server.siril import SirilBinary, SirilResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUCCESS_STDOUT = (
    "progress: Sequence processing succeeded., 0.00%\n"
    "log: Sequence processing succeeded.\n"
    "log: Execution time: 47.75 s\n"
    "Writing sequence file bkg_rs_pp_light_.seq\n"
)

# Siril 1.4.3 stdout from a successful `stack` command (verified on live box).
_STACK_SUCCESS_STDOUT = (
    "log: Stacking sequence light_\n"
    "log: Starting stacking...\n"
    "progress: Median stacking in progress..., 0.00%\n"
    "progress: Finalizing stacking..., 94.12%\n"
    "progress: Median stacking complete., 100.00%\n"
    "log: Median stacking complete. 3 images have been stacked.\n"
    "log: Saving FITS: file /data/cache/abc123/image.fit, 1 layer(s), 32x32 pixels, 32 bits\n"
    "log: Stacked sequence successfully.\n"
    "log: Execution time: 15.94 ms\n"
)

_FAILURE_STDOUT = (
    "log: Error: could not open sequence\n"
    "log: Execution time: 0.01 s\n"
)


def _make_result(*, returncode: int, stdout: str = "") -> SirilResult:
    return SirilResult(returncode=returncode, stdout=stdout, stderr="", ssf="")


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: None,
        log=logging.getLogger("test"),
    )


class FakeRuntime:
    """Fake SirilRuntime whose run() outcome is scripted by the test."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", on_run: Any | None = None) -> None:
        self.binary = SirilBinary(path=Path("/fake/siril"), source="env")
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[dict[str, Any]] = []
        self._on_run = on_run

    def run(self, commands, *, working_dir=None, on_log=None, timeout=None,
            require_version="1.4.0", cancel=None) -> SirilResult:
        self.calls.append({"commands": list(commands), "working_dir": working_dir})
        if self._on_run is not None:
            self._on_run(commands, working_dir)
        return SirilResult(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr="",
            ssf="\n".join(commands),
        )


# ---------------------------------------------------------------------------
# _check_siril_seq_result: unit tests for all four cases (FITSEQ mode)
# ---------------------------------------------------------------------------


class TestCheckSirilSeqResultFitseq:
    """Tests for _check_siril_seq_result with fitseq=True."""

    def test_rc0_outputs_present_succeeds(self, tmp_path: Path) -> None:
        """returncode=0 + success message + outputs present -> success."""
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()
        (seq_out / "bkg_pp_light.fit").write_bytes(b"FAKE")

        result = _make_result(returncode=0, stdout=_SUCCESS_STDOUT)
        wrote = _check_siril_seq_result(
            result,
            node_name="seq_bg_extract",
            seq_out=seq_out,
            out_basename="bkg_pp_light",
            fitseq=True,
            min_count=1,
        )
        assert wrote == "bkg_pp_light.fit"

    def test_rc_minus11_success_message_outputs_present_warns_and_succeeds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """returncode=-11 + success message + outputs present -> success WITH warning."""
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()
        (seq_out / "bkg_pp_light.fit").write_bytes(b"FAKE")

        result = _make_result(returncode=-11, stdout=_SUCCESS_STDOUT)
        with caplog.at_level(logging.WARNING, logger="nodes._seq_runner"):
            wrote = _check_siril_seq_result(
                result,
                node_name="seq_bg_extract",
                seq_out=seq_out,
                out_basename="bkg_pp_light",
                fitseq=True,
                min_count=1,
            )
        assert wrote == "bkg_pp_light.fit"
        assert any("segfault" in record.message for record in caplog.records)
        assert any("-11" in record.message for record in caplog.records)

    def test_rc_minus11_success_message_outputs_missing_raises(self, tmp_path: Path) -> None:
        """returncode=-11 + success message + outputs MISSING -> still failure."""
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()
        # No output files written.

        result = _make_result(returncode=-11, stdout=_SUCCESS_STDOUT)
        with pytest.raises(RuntimeError, match="exited -11"):
            _check_siril_seq_result(
                result,
                node_name="seq_bg_extract",
                seq_out=seq_out,
                out_basename="bkg_pp_light",
                fitseq=True,
                min_count=1,
            )

    def test_rc_minus11_no_success_message_raises(self, tmp_path: Path) -> None:
        """returncode=-11 + NO success message -> still failure."""
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()
        (seq_out / "bkg_pp_light.fit").write_bytes(b"FAKE")

        result = _make_result(returncode=-11, stdout=_FAILURE_STDOUT)
        with pytest.raises(RuntimeError, match="exited -11"):
            _check_siril_seq_result(
                result,
                node_name="seq_bg_extract",
                seq_out=seq_out,
                out_basename="bkg_pp_light",
                fitseq=True,
                min_count=1,
            )

    def test_rc1_failure_raises(self, tmp_path: Path) -> None:
        """returncode=1 + failure -> still failure."""
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()

        result = _make_result(returncode=1, stdout=_FAILURE_STDOUT)
        with pytest.raises(RuntimeError, match="exited 1"):
            _check_siril_seq_result(
                result,
                node_name="seq_bg_extract",
                seq_out=seq_out,
                out_basename="bkg_pp_light",
                fitseq=True,
                min_count=1,
            )

    def test_rc0_outputs_missing_raises(self, tmp_path: Path) -> None:
        """returncode=0 but outputs missing -> still failure."""
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()

        result = _make_result(returncode=0, stdout=_SUCCESS_STDOUT)
        with pytest.raises(RuntimeError, match="missing"):
            _check_siril_seq_result(
                result,
                node_name="seq_bg_extract",
                seq_out=seq_out,
                out_basename="bkg_pp_light",
                fitseq=True,
                min_count=1,
            )


# ---------------------------------------------------------------------------
# _check_siril_seq_result: per-frame mode
# ---------------------------------------------------------------------------


class TestCheckSirilSeqResultPerFrame:
    """Tests for _check_siril_seq_result with fitseq=False."""

    def _write_frames(self, seq_out: Path, basename: str, n: int) -> None:
        for i in range(1, n + 1):
            (seq_out / f"{basename}_{i:05d}.fit").write_bytes(b"FAKE")

    def test_rc0_outputs_present_succeeds(self, tmp_path: Path) -> None:
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()
        self._write_frames(seq_out, "pp_light", 3)

        result = _make_result(returncode=0, stdout=_SUCCESS_STDOUT)
        wrote = _check_siril_seq_result(
            result,
            node_name="calibrate",
            seq_out=seq_out,
            out_basename="pp_light",
            fitseq=False,
            min_count=3,
        )
        assert wrote == "3 frames"

    def test_rc_minus11_success_message_outputs_present_warns_and_succeeds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()
        self._write_frames(seq_out, "pp_light", 5)

        result = _make_result(returncode=-11, stdout=_SUCCESS_STDOUT)
        with caplog.at_level(logging.WARNING, logger="nodes._seq_runner"):
            wrote = _check_siril_seq_result(
                result,
                node_name="calibrate",
                seq_out=seq_out,
                out_basename="pp_light",
                fitseq=False,
                min_count=5,
            )
        assert wrote == "5 frames"
        assert any("segfault" in record.message for record in caplog.records)

    def test_rc_minus11_outputs_missing_raises(self, tmp_path: Path) -> None:
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()
        # Outputs absent.

        result = _make_result(returncode=-11, stdout=_SUCCESS_STDOUT)
        with pytest.raises(RuntimeError, match="exited -11"):
            _check_siril_seq_result(
                result,
                node_name="calibrate",
                seq_out=seq_out,
                out_basename="pp_light",
                fitseq=False,
                min_count=3,
            )

    def test_rc_minus11_no_success_message_raises(self, tmp_path: Path) -> None:
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()
        self._write_frames(seq_out, "pp_light", 3)

        result = _make_result(returncode=-11, stdout=_FAILURE_STDOUT)
        with pytest.raises(RuntimeError, match="exited -11"):
            _check_siril_seq_result(
                result,
                node_name="calibrate",
                seq_out=seq_out,
                out_basename="pp_light",
                fitseq=False,
                min_count=3,
            )

    def test_rc0_outputs_missing_raises(self, tmp_path: Path) -> None:
        seq_out = tmp_path / "seq_out"
        seq_out.mkdir()

        result = _make_result(returncode=0, stdout=_SUCCESS_STDOUT)
        with pytest.raises(RuntimeError, match="no pp_light"):
            _check_siril_seq_result(
                result,
                node_name="calibrate",
                seq_out=seq_out,
                out_basename="pp_light",
                fitseq=False,
                min_count=3,
            )


# ---------------------------------------------------------------------------
# run_siril_on_sequence integration: segfault tolerance end-to-end
# ---------------------------------------------------------------------------


class TestRunSirilOnSequenceSegfaultTolerance:
    """Integration tests through run_siril_on_sequence for the segfault path."""

    def test_segfault_rc_fitseq_succeeds_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full path: seq node gets rc=-11, success marker present, output present."""
        seq_in = tmp_path / "in"
        seq_in.mkdir()
        (seq_in / "pp_light.fit").write_bytes(b"FAKE")
        seq_out = tmp_path / "out" / "sequence"

        def on_run(cmds, wd):
            seq_out.mkdir(parents=True, exist_ok=True)
            (seq_out / "bkg_pp_light.fit").write_bytes(b"OUTPUT")

        fake_rt = FakeRuntime(returncode=-11, stdout=_SUCCESS_STDOUT, on_run=on_run)

        with caplog.at_level(logging.WARNING, logger="nodes._seq_runner"):
            wrote = run_siril_on_sequence(
                node_name="seq_bg_extract",
                seq_in=seq_in,
                seq_out=seq_out,
                commands=["cd /fake", "seqsubsky pp_light 1 -samples=10"],
                basename="pp_light",
                out_basename="bkg_pp_light",
                fitseq=True,
                ctx=_ctx(tmp_path),
                runtime=fake_rt,
            )

        assert wrote == "bkg_pp_light.fit"
        assert any("segfault" in r.message for r in caplog.records)
        # Staged symlink must be cleaned up even on the segfault path.
        remaining = [p.name for p in seq_out.iterdir()]
        assert "bkg_pp_light.fit" in remaining
        assert "pp_light.fit" not in remaining

    def test_segfault_rc_no_success_message_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rc=-11 without success marker must still raise."""
        seq_in = tmp_path / "in"
        seq_in.mkdir()
        (seq_in / "pp_light.fit").write_bytes(b"FAKE")
        seq_out = tmp_path / "out" / "sequence"

        def on_run(cmds, wd):
            seq_out.mkdir(parents=True, exist_ok=True)
            (seq_out / "bkg_pp_light.fit").write_bytes(b"OUTPUT")

        fake_rt = FakeRuntime(returncode=-11, stdout=_FAILURE_STDOUT, on_run=on_run)

        with pytest.raises(RuntimeError, match="exited -11"):
            run_siril_on_sequence(
                node_name="seq_bg_extract",
                seq_in=seq_in,
                seq_out=seq_out,
                commands=["cd /fake", "seqsubsky pp_light 1 -samples=10"],
                basename="pp_light",
                out_basename="bkg_pp_light",
                fitseq=True,
                ctx=_ctx(tmp_path),
                runtime=fake_rt,
            )


# ---------------------------------------------------------------------------
# _check_siril_stack_result: unit tests for all five cases
# ---------------------------------------------------------------------------


class TestCheckSirilStackResult:
    """Tests for _check_siril_stack_result (stack-specific segfault tolerance).

    The five cases mirror the spec in the issue:
      1. rc=0 + success log + output present -> success
      2. rc=-11 + success log + output present -> success with warning
      3. rc=-11 + success log + output MISSING -> failure
      4. rc=-11 + NO success log -> failure
      5. rc=1 -> failure
    """

    def test_rc0_success_log_output_present_succeeds(self, tmp_path: Path) -> None:
        """rc=0 + success log + output present -> succeeds without error."""
        out_image = tmp_path / "image.fit"
        out_image.write_bytes(b"FAKEFIT")

        result = _make_result(returncode=0, stdout=_STACK_SUCCESS_STDOUT)
        # Must not raise.
        _check_siril_stack_result(result, node_name="seq_stack", out_image=out_image)

    def test_rc_minus11_success_log_output_present_warns_and_succeeds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """rc=-11 + success log + output present -> success WITH warning (segfault path)."""
        out_image = tmp_path / "image.fit"
        out_image.write_bytes(b"FAKEFIT")

        result = _make_result(returncode=-11, stdout=_STACK_SUCCESS_STDOUT)
        with caplog.at_level(logging.WARNING, logger="nodes._seq_runner"):
            _check_siril_stack_result(result, node_name="seq_stack", out_image=out_image)

        assert any("segfault" in record.message for record in caplog.records)
        assert any("-11" in record.message for record in caplog.records)

    def test_rc_minus11_success_log_output_missing_raises(self, tmp_path: Path) -> None:
        """rc=-11 + success log + output MISSING -> still a real failure."""
        out_image = tmp_path / "image.fit"
        # Deliberately NOT written.

        result = _make_result(returncode=-11, stdout=_STACK_SUCCESS_STDOUT)
        with pytest.raises(RuntimeError, match="exited -11"):
            _check_siril_stack_result(result, node_name="seq_stack", out_image=out_image)

    def test_rc_minus11_no_success_log_raises(self, tmp_path: Path) -> None:
        """rc=-11 + NO success log -> real failure regardless of output file."""
        out_image = tmp_path / "image.fit"
        out_image.write_bytes(b"FAKEFIT")

        result = _make_result(returncode=-11, stdout=_FAILURE_STDOUT)
        with pytest.raises(RuntimeError, match="exited -11"):
            _check_siril_stack_result(result, node_name="seq_stack", out_image=out_image)

    def test_rc1_raises(self, tmp_path: Path) -> None:
        """rc=1 (real Siril error, not segfault) -> failure."""
        out_image = tmp_path / "image.fit"

        result = _make_result(returncode=1, stdout=_FAILURE_STDOUT)
        with pytest.raises(RuntimeError, match="exited 1"):
            _check_siril_stack_result(result, node_name="seq_stack", out_image=out_image)
