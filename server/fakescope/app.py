"""The fake scope over HTTP, with the relay's paths, operation ids and error shape.

Run it with ``python -m server.fakescope`` and point astrolab's scope URL at it. Settings
come from the environment: FAKESCOPE_SEED picks the yard's weather, FAKESCOPE_LATENCY
scales how long slow operations take (1 is like the real scope, 0 is instant).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server.fakescope.core import FakeScope, ScopeError
from server.fakescope.scene import Scene


class GotoIn(BaseModel):
    name: str
    ra_hours: float
    dec_degrees: float


class PointIn(BaseModel):
    pitch: float
    rotation: float | None = None


class CaptureIn(BaseModel):
    frames: int = Field(ge=1, le=500)
    exposure: str = "15"
    gain: int = 60
    filter: str = "duo"


class SessionIn(BaseModel):
    session: str


def create_app(scope: FakeScope | None = None) -> FastAPI:
    if scope is None:
        scene = Scene(seed=int(os.environ.get("FAKESCOPE_SEED", "1")))
        scope = FakeScope(scene, latency_scale=float(os.environ.get("FAKESCOPE_LATENCY", "1")))
    app = FastAPI(title="fakescope", version=scope.capabilities()["spec_version"])
    app.state.scope = scope

    @app.exception_handler(ScopeError)
    def _scope_error(_req: Request, exc: ScopeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status)

    @app.get("/api/capabilities", operation_id="getCapabilities")
    def capabilities() -> dict:
        return scope.capabilities()

    @app.get("/api/status", operation_id="getStatus")
    def status() -> dict:
        return scope.status()

    @app.get("/api/sessions", operation_id="listSessions")
    def sessions() -> dict:
        return {"sessions": [e["name"] for e in scope.list_files("")["entries"]]}

    @app.post("/api/sessions/delete", operation_id="deleteSession")
    def delete_session(p: SessionIn) -> dict:
        return scope.delete_session(p.session)

    @app.get("/api/files", operation_id="listFiles")
    def files(path: str = "") -> dict:
        return scope.list_files(path)

    @app.get("/api/files/chunk", operation_id="readFileChunk")
    def chunk(
        path: str, offset: int = Query(0, ge=0), length: int = Query(1048576, gt=0, le=4194304)
    ) -> dict:
        return scope.read_chunk(path, offset, length)

    @app.get("/api/frame", operation_id="getFrame")
    def frame(camera: str = "wide") -> dict:
        return scope.frame(camera)

    @app.post("/api/prepare", operation_id="prepareScope")
    def prepare() -> dict:
        return scope.prepare()

    @app.post("/api/point", operation_id="pointScope")
    def point(p: PointIn) -> dict:
        return scope.point(p.pitch, p.rotation)

    @app.post("/api/close_barrel", operation_id="closeBarrel")
    def close_barrel() -> dict:
        return scope.close_barrel()

    @app.post("/api/calibrate", operation_id="calibrateScope")
    def calibrate() -> dict:
        return scope.calibrate()

    @app.post("/api/goto", operation_id="gotoTarget")
    def goto(p: GotoIn) -> dict:
        return scope.goto(p.name, p.ra_hours, p.dec_degrees)

    @app.post("/api/capture", operation_id="startCapture", status_code=202)
    def capture(p: CaptureIn) -> dict:
        return scope.start_capture(p.frames, p.exposure, p.gain, p.filter)

    @app.post("/api/capture/stop", operation_id="stopCapture")
    def stop() -> dict:
        return scope.stop_capture()

    return app
