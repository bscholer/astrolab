"""Unit tests for server.scan_state."""

from __future__ import annotations

import importlib


def _fresh_module():
    """Reload scan_state to get a clean global state for each test."""
    import server.scan_state as m
    importlib.reload(m)
    return m


def test_start_flips_running() -> None:
    m = _fresh_module()
    assert not m.snapshot()["running"]
    result = m.start()
    assert result is True
    assert m.snapshot()["running"] is True
    assert m.snapshot()["started_at"] is not None


def test_start_returns_false_when_already_running() -> None:
    m = _fresh_module()
    m.start()
    result = m.start()
    assert result is False
    # State should still be running (not reset by the rejected start).
    assert m.snapshot()["running"] is True


def test_update_sets_discovered_and_path() -> None:
    m = _fresh_module()
    m.start()
    m.update(42, "/captures/foo/bar.fits")
    s = m.snapshot()
    assert s["discovered"] == 42
    assert s["current_path"] == "/captures/foo/bar.fits"


def test_finish_flips_running_false() -> None:
    m = _fresh_module()
    m.start()
    m.update(10, "/some/path.fits")
    m.finish()
    s = m.snapshot()
    assert s["running"] is False
    assert s["finished_at"] is not None
    assert s["current_path"] is None


def test_finish_captures_last_stats() -> None:
    m = _fresh_module()
    m.start()

    class FakeStats:
        discovered = 100
        inserted = 80
        updated = 5
        skipped_unchanged = 15
        removed = 2
        failed = 1
        masters_inserted = 3
        masters_updated = 0
        masters_removed = 0
        masters_skipped = 1

    m.finish(stats=FakeStats())
    s = m.snapshot()
    assert s["running"] is False
    assert s["last_stats"] is not None
    assert s["last_stats"]["inserted"] == 80
    assert s["last_stats"]["masters_inserted"] == 3
    assert s["error"] is None


def test_finish_with_error() -> None:
    m = _fresh_module()
    m.start()
    m.finish(error="boom")
    s = m.snapshot()
    assert s["running"] is False
    assert s["error"] == "boom"
    assert s["finished_at"] is not None
