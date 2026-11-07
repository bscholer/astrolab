"""Manual scope actions: one at a time, and a failure is reported."""

from __future__ import annotations

import threading
import time

import pytest

from server.scope_control import ControlBusy, ScopeControl


class Slow:
    def __init__(self, fail=False):
        self.release = threading.Event()
        self.fail = fail
        self.ops = []

    def call(self, op, payload=None, timeout=None):
        self.ops.append((op, timeout))
        self.release.wait(2)
        if self.fail:
            raise RuntimeError("calibration failed")
        return {}


def _wait(control):
    for _ in range(100):
        if control.snapshot()["state"] != "running":
            return
        time.sleep(0.02)


def test_second_action_is_refused_while_one_runs():
    control, client = ScopeControl(), Slow()
    control.start(client, "calibrate")
    with pytest.raises(ControlBusy):
        control.start(client, "close")
    client.release.set()
    _wait(control)
    assert control.snapshot()["state"] == "done"
    assert client.ops == [("calibrateScope", 330)]


def test_failure_is_kept_for_the_page():
    control, client = ScopeControl(), Slow(fail=True)
    client.release.set()
    control.start(client, "close")
    _wait(control)
    snap = control.snapshot()
    assert snap["state"] == "failed" and snap["error"] == "calibration failed"
    assert client.ops == [("closeBarrel", 90)]
