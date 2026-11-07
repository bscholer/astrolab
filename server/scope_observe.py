"""Run a scope observation in the background: prepare, calibrate, slew, capture."""

from __future__ import annotations

import threading

from server.scope_service import SLEW_TIMEOUT_S, ScopeServiceClient, ScopeServiceError

PREPARE_TIMEOUT_S = 90
CALIBRATE_TIMEOUT_S = 330


class ObserveBusy(Exception):
    pass


class Observer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict = {"state": "idle"}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def start(
        self, client: ScopeServiceClient, name: str, ra_deg: float, dec_deg: float, frames: int,
        exposure_s: int,
    ) -> None:
        with self._lock:
            if self.state.get("state") == "running":
                raise ObserveBusy(f"already observing {self.state.get('target')}")
            self.state = {"state": "running", "step": "starting", "target": name, "error": None}
        self._thread = threading.Thread(
            target=self._run,
            args=(client, name, ra_deg, dec_deg, frames, exposure_s),
            name="astrolab-observe",
            daemon=True,
        )
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self, client, name, ra_deg, dec_deg, frames, exposure_s) -> None:
        try:
            if client.has("setup"):
                self._set(step="preparing")
                client.call("prepareScope", timeout=PREPARE_TIMEOUT_S)
                self._set(step="calibrating")
                client.call("calibrateScope", timeout=CALIBRATE_TIMEOUT_S)
            self._set(step="slewing")
            client.call(
                "gotoTarget",
                {"name": name, "ra_hours": ra_deg / 15, "dec_degrees": dec_deg},
                timeout=SLEW_TIMEOUT_S,
            )
            self._set(step="starting capture")
            client.call("startCapture", {"frames": frames, "exposure": str(exposure_s)})
            self._set(state="done", step="capturing")
        except ScopeServiceError as exc:
            self._set(state="failed", error=f"{self.snapshot().get('step')}: {exc}")
        except Exception as exc:
            self._set(state="failed", error=str(exc))
