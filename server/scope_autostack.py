"""Stack a scope session once its frames stop arriving."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from server.telemetry import traced

log = logging.getLogger(__name__)

QUIET_S = 5 * 60


class AutoStacker:
    def __init__(
        self,
        get_enabled: Callable[[], bool],
        find_session: Callable[[str], tuple[int, int] | None],
        submit: Callable[[int], str],
        quiet_s: float = QUIET_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """find_session maps a folder name to (session_id, light_count). submit returns a job id."""
        self._get_enabled = get_enabled
        self._find_session = find_session
        self._submit = submit
        self.quiet_s = quiet_s
        self._clock = clock
        self._lock = threading.Lock()
        self._last_arrival: dict[str, float] = {}
        self._stacked_count: dict[str, int] = {}

    def touch(self, folder: str) -> None:
        with self._lock:
            self._last_arrival[folder] = self._clock()

    @traced("scope.autostack")
    def tick(self) -> list[str]:
        """Submit a job for each quiet folder with frames not yet stacked. Return job ids."""
        if not self._get_enabled():
            return []
        with self._lock:
            quiet = [
                f for f, at in self._last_arrival.items() if self._clock() - at >= self.quiet_s
            ]
        jobs = []
        for folder in quiet:
            try:
                found = self._find_session(folder)
                if found is None:
                    continue
                session_id, count = found
                if count <= self._stacked_count.get(folder, 0):
                    with self._lock:
                        self._last_arrival.pop(folder, None)
                    continue
                jobs.append(self._submit(session_id))
                self._stacked_count[folder] = count
                with self._lock:
                    self._last_arrival.pop(folder, None)
            except Exception as exc:
                log.warning("auto-stack of %s failed: %s", folder, exc)
        return jobs

    def start(self, interval_s: float = 60) -> None:
        def loop() -> None:
            while True:
                time.sleep(interval_s)
                self.tick()

        threading.Thread(target=loop, name="astrolab-autostack", daemon=True).start()
