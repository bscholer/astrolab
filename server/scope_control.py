"""Run one manual scope action in the background: prepare, calibrate, or close the barrel."""

from __future__ import annotations

import threading

from server.scope_service import ScopeServiceClient

ACTIONS = {
    "prepare": ("prepareScope", 90, "Preparing"),
    "calibrate": ("calibrateScope", 330, "Calibrating"),
    "close": ("closeBarrel", 90, "Closing the barrel"),
}


class ControlBusy(Exception):
    pass


class ScopeControl:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state: dict = {"state": "idle"}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def start(self, client: ScopeServiceClient, action: str) -> None:
        if action not in ACTIONS:
            raise ValueError(f"unknown action: {action}")
        with self._lock:
            if self.state.get("state") == "running":
                raise ControlBusy(f"{self.state['label']} is still running")
            label = ACTIONS[action][2]
            self.state = {"state": "running", "action": action, "label": label, "error": None}
        threading.Thread(
            target=self._run, args=(client, action), name="astrolab-control", daemon=True
        ).start()

    def _run(self, client, action) -> None:
        op, timeout = ACTIONS[action][:2]
        try:
            client.call(op, timeout=timeout)
            with self._lock:
                self.state["state"] = "done"
        except Exception as exc:
            with self._lock:
                self.state.update(state="failed", error=str(exc))
