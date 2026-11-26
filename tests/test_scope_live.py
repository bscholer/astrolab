"""Live video: one reader per camera shared by every viewer, started by the first and stopped
after the last; MJPEG parts as frames arrive; a clear refusal when the scope has no video."""

from __future__ import annotations

import asyncio
import time

import numpy as np
from fastapi.testclient import TestClient

import server.api as api
from server import scope_live
from server.scope_live import LiveCamera, mjpeg, rtsp_url


class FakeCapture:
    """Thirty frames a second of a growing gray level, like an open RTSP stream."""

    opened = 0

    def __init__(self, url, ok=True):
        FakeCapture.opened += 1
        self.ok = ok
        self.n = 0

    def isOpened(self):
        return self.ok

    def grab(self):
        time.sleep(1 / 30)
        self.n += 1
        return True

    def retrieve(self):
        return True, np.full((1080, 1920, 3), self.n % 255, np.uint8)

    def release(self):
        pass


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_the_stream_is_at_the_relay_host():
    assert rtsp_url("http://192.168.1.140:9902", "wide") == "rtsp://192.168.1.140:554/ch1/stream0"
    assert rtsp_url("http://relay.local:9902/", "tele") == "rtsp://relay.local:554/ch0/stream0"
    assert rtsp_url(None, "wide") is None and rtsp_url("http://x:1", "nope") is None


def test_viewers_share_one_reader_that_stops_after_the_last_leaves():
    FakeCapture.opened = 0
    cam = LiveCamera("wide", opener=FakeCapture, fps=15, idle_s=0.3)
    cam.watch("rtsp://x")
    cam.watch("rtsp://x")
    assert _wait(lambda: cam.seq >= 5)
    assert FakeCapture.opened == 1 and cam.viewers == 2
    jpeg = cam.jpeg
    assert jpeg[:2] == b"\xff\xd8"  # a JPEG, shrunk for the browser
    import cv2

    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), 1).shape[1] == scope_live.WIDTH
    cam.leave()
    cam.leave()
    assert _wait(lambda: not cam._thread.is_alive(), timeout=3)


def test_frames_are_sent_on_at_the_chosen_rate_not_the_scopes():
    cam = LiveCamera("wide", opener=FakeCapture, fps=10, idle_s=0.1)
    cam.watch("rtsp://x")
    assert _wait(lambda: cam.seq >= 1)
    s0, t0 = cam.seq, time.monotonic()
    time.sleep(1.0)
    rate = (cam.seq - s0) / (time.monotonic() - t0)
    cam.leave()
    assert 6 <= rate <= 12


def test_a_stream_that_will_not_open_reports_why():
    cam = LiveCamera("tele", opener=lambda url: FakeCapture(url, ok=False), idle_s=0.1)
    cam.watch("rtsp://x")
    assert _wait(lambda: cam.error is not None)
    assert "prepared" in cam.error and cam.jpeg is None
    cam.leave()


def test_mjpeg_sends_each_new_frame_once_and_lets_go_when_the_viewer_leaves():
    cam = LiveCamera("wide", opener=FakeCapture, fps=30, idle_s=0.1)
    cam.watch("rtsp://x")
    assert _wait(lambda: cam.jpeg is not None)
    parts = []
    gone = {"now": False}

    async def disconnected():
        return gone["now"]

    async def read():
        async for p in mjpeg(cam, disconnected):
            parts.append(p)
            if len(parts) == 3:
                gone["now"] = True

    asyncio.run(asyncio.wait_for(read(), 5))
    assert len(parts) == 3
    assert all(p.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n") for p in parts)
    assert len({p for p in parts}) == 3
    assert cam.viewers == 0


def test_mjpeg_ends_when_the_camera_stalls():
    cam = LiveCamera("wide")
    cam.jpeg, cam.seq = b"\xff\xd8x", 1
    cam._viewers = 1

    async def never():
        return False

    async def read():
        return [p async for p in mjpeg(cam, never, stall_s=0.2)]

    assert len(asyncio.run(asyncio.wait_for(read(), 3))) == 1
    assert cam.viewers == 0


def test_endpoint_refuses_unknown_cameras_and_a_missing_scope(tmp_path, monkeypatch):
    api.job_manager.reset_for_tests(db_path=tmp_path / "c.sqlite")
    client = TestClient(api.app)
    assert client.get("/api/scope/live/side.mjpg").status_code == 404
    assert client.get("/api/scope/live/wide.mjpg").status_code == 400


def test_endpoint_says_503_when_the_video_does_not_start(tmp_path, monkeypatch):
    api.job_manager.reset_for_tests(db_path=tmp_path / "c.sqlite")
    api.set_setting(api.SETTING_SCOPE_URL, "http://127.0.0.1:9", db_path=api.job_manager.db_path)
    cam = LiveCamera("wide", opener=lambda url: FakeCapture(url, ok=False), idle_s=0.1)
    monkeypatch.setitem(scope_live.cameras, "wide", cam)
    monkeypatch.setitem(scope_live.FIRST_FRAME_S, "wide", 0.5)
    r = TestClient(api.app).get("/api/scope/live/wide.mjpg")
    assert r.status_code == 503 and "prepared" in r.json()["detail"]
    assert cam.viewers == 0
