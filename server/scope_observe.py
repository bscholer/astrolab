"""Run a scope observation in the background: prepare, calibrate, slew, capture."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from server.scope_service import SLEW_TIMEOUT_S, ScopeServiceClient, ScopeServiceError

log = logging.getLogger(__name__)

PREPARE_TIMEOUT_S = 90
CALIBRATE_TIMEOUT_S = 450


class ObserveBusy(Exception):
    pass


class Observer:
    def __init__(self, on_pointed: Callable[[dict], None] | None = None) -> None:
        """on_pointed gets the motor angles and target after each successful slew."""
        self._on_pointed = on_pointed or (lambda _pair: None)
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
        exposure_s: int, gain: int = 60, filter_name: str = "astro",
    ) -> None:
        with self._lock:
            if self.state.get("state") == "running":
                raise ObserveBusy(f"already observing {self.state.get('target')}")
            self.state = {"state": "running", "step": "starting", "target": name, "error": None}
        self._thread = threading.Thread(
            target=self._run,
            args=(client, name, ra_deg, dec_deg, frames, exposure_s, gain, filter_name),
            name="astrolab-observe",
            daemon=True,
        )
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _record_pointing(self, client, name, ra_deg, dec_deg) -> None:
        try:
            status = client.call("getStatus") or {}
            pitch, rotation = status.get("pitch"), status.get("rotation")
            if pitch is not None and rotation is not None:
                self._on_pointed(
                    {
                        "target": name,
                        "ra_deg": ra_deg,
                        "dec_deg": dec_deg,
                        "pitch": pitch,
                        "rotation": rotation,
                        "t": time.time(),
                    }
                )
        except Exception:
            log.warning("could not record the pointing of %s", name, exc_info=True)

    def _run(self, client, name, ra_deg, dec_deg, frames, exposure_s, gain, filter_name) -> None:
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
            self._record_pointing(client, name, ra_deg, dec_deg)
            self._set(step="starting capture")
            client.call(
                "startCapture",
                {
                    "frames": frames,
                    "exposure": str(exposure_s),
                    "gain": gain,
                    "filter": filter_name,
                },
            )
            self._set(state="done", step="capturing")
        except ScopeServiceError as exc:
            self._set(state="failed", error=f"{self.snapshot().get('step')}: {exc}")
        except Exception as exc:
            self._set(state="failed", error=str(exc))
