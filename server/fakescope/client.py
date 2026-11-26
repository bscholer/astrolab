"""Talk to a FakeScope in-process with the same call() and has() as ScopeServiceClient."""

from __future__ import annotations

from typing import Any

from server.fakescope.core import FakeScope, ScopeError
from server.scope_service import ScopeServiceError


class InProcessClient:
    def __init__(self, scope: FakeScope) -> None:
        self.scope = scope
        s = scope
        self._ops = {
            "getStatus": lambda p: s.status(),
            "prepareScope": lambda p: s.prepare(),
            "calibrateScope": lambda p: s.calibrate(),
            "gotoTarget": lambda p: s.goto(p["name"], p["ra_hours"], p["dec_degrees"]),
            "pointScope": lambda p: s.point(p["pitch"], p.get("rotation")),
            "closeBarrel": lambda p: s.close_barrel(),
            "startCapture": lambda p: s.start_capture(
                p["frames"], str(p.get("exposure", "15")), p.get("gain", 60), p.get("filter", "duo")
            ),
            "stopCapture": lambda p: s.stop_capture(),
            "listFiles": lambda p: s.list_files(p.get("path", "")),
            "readFileChunk": lambda p: s.read_chunk(
                p["path"], p.get("offset", 0), p.get("length", 1048576)
            ),
            "deleteSession": lambda p: s.delete_session(p["session"]),
            "getFrame": lambda p: s.frame(p.get("camera", "wide")),
        }

    def info(self) -> dict:
        return self.scope.capabilities()

    def has(self, capability: str) -> bool:
        return capability in self.scope.capabilities()["capabilities"]

    def call(self, operation_id: str, payload: dict | None = None, timeout: float = 0) -> Any:
        if operation_id not in self._ops:
            raise ScopeServiceError(f"service has no operation {operation_id}", code="unsupported")
        try:
            return self._ops[operation_id](payload or {})
        except ScopeError as exc:
            raise ScopeServiceError(str(exc), code=exc.code, status=exc.status) from exc
