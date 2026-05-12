"""GraxpertParams behavior tests.

The interesting part is the runtime-detected `use_gpu` default. GraXpert's
bundled onnxruntime segfaults trying to init CUDA when no NVIDIA libraries
are reachable, so the param defaults to True on hosts with /dev/nvidia0 and
False otherwise.
"""

from __future__ import annotations

import pytest

from nodes.basic.graxpert import GraxpertParams


def test_use_gpu_defaults_true_when_nvidia_device_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When /dev/nvidia0 exists, use_gpu defaults to True."""
    monkeypatch.setattr("nodes.basic.graxpert._gpu_available", lambda: True)
    assert GraxpertParams().use_gpu is True


def test_use_gpu_defaults_false_when_no_nvidia_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When /dev/nvidia0 is absent, use_gpu defaults to False so GraXpert
    doesn't segfault trying to load CUDA on a CPU-only host."""
    monkeypatch.setattr("nodes.basic.graxpert._gpu_available", lambda: False)
    assert GraxpertParams().use_gpu is False


def test_use_gpu_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user passing use_gpu=True or False bypasses the auto-detect."""
    monkeypatch.setattr("nodes.basic.graxpert._gpu_available", lambda: False)
    assert GraxpertParams(use_gpu=True).use_gpu is True
    monkeypatch.setattr("nodes.basic.graxpert._gpu_available", lambda: True)
    assert GraxpertParams(use_gpu=False).use_gpu is False
