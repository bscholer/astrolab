"""Live video from the scope's cameras, as MJPEG a browser shows in a plain <img>.

The scope streams each camera over RTSP as 1920x1080 HEVC at 30 fps, which most browsers
cannot play. One reader per camera decodes it, shrinks it and keeps the newest JPEG; every
viewer of that camera gets the same frames, so a second tab costs nothing more on the scope
or here. A reader starts with its first viewer and stops a little after its last one leaves.

The relay forwards the scope's RTSP port, so the stream is at the relay's host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np

log = logging.getLogger(__name__)

RTSP_PORT = 554
RTSP_PATHS = {"wide": "ch1/stream0", "tele": "ch0/stream0"}
WIDTH = 960
FPS = 15.0  # the scope sends 30: every second frame
JPEG_QUALITY = 72
IDLE_S = 15.0  # keep reading this long after the last viewer, so a reload is instant
OPEN_TIMEOUT_MS = 10_000
# The tele stream can wait for a keyframe, about every 40 s while the scope captures.
READ_TIMEOUT_MS = 60_000
RETRY_S = (2.0, 5.0, 10.0)
FIRST_FRAME_S = {"wide": 15.0, "tele": 75.0}
STALL_S = 90.0  # a viewer whose camera sends nothing this long is let go
BOUNDARY = "frame"

# The scope's video runs over TCP: UDP drops frames across the Wi-Fi hop.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


def rtsp_url(service_url: str | None, camera: str) -> str | None:
    """Where a camera's stream is, from the relay's address."""
    if not service_url or camera not in RTSP_PATHS:
        return None
    host = urlparse(service_url).hostname
    return f"rtsp://{host}:{RTSP_PORT}/{RTSP_PATHS[camera]}" if host else None


def open_capture(url: str) -> Any:
    params = [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
        OPEN_TIMEOUT_MS,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
        READ_TIMEOUT_MS,
    ]
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)


def encode(frame: np.ndarray, width: int = WIDTH) -> bytes:
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError("could not encode a frame")
    return jpeg.tobytes()


class LiveCamera:
    """The newest frame of one camera, read while anyone is watching."""

    def __init__(
        self,
        name: str,
        opener: Callable[[str], Any] = open_capture,
        fps: float = FPS,
        idle_s: float = IDLE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._opener = opener
        self._fps = fps
        self._idle_s = idle_s
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._url: str | None = None
        self._viewers = 0
        self._last_viewer = 0.0
        self.seq = 0
        self.jpeg: bytes | None = None
        self.error: str | None = None

    def watch(self, url: str) -> None:
        """Count a viewer in, and start reading if nobody was."""
        with self._lock:
            self._viewers += 1
            if url != self._url:
                self._url = url
                self.jpeg, self.error = None, None
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name=f"astrolab-live-{self.name}", daemon=True
                )
                self._thread.start()

    def leave(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            self._last_viewer = self._clock()

    @property
    def viewers(self) -> int:
        return self._viewers

    def _wanted(self) -> bool:
        with self._lock:
            return self._viewers > 0 or self._clock() - self._last_viewer < self._idle_s

    def _run(self) -> None:
        attempt = 0
        while self._wanted():
            url = self._url
            cap = self._opener(url) if url else None
            if cap is None or not cap.isOpened():
                self.error = "the scope's video did not open (is it prepared?)"
                if cap is not None:
                    cap.release()
                time.sleep(RETRY_S[min(attempt, len(RETRY_S) - 1)])
                attempt += 1
                continue
            log.info("live %s: reading %s", self.name, url)
            last = 0.0
            try:
                while self._wanted() and url == self._url:
                    if not cap.grab():
                        self.error = "the scope's video stopped"
                        break
                    now = self._clock()
                    if now - last < 1.0 / self._fps:
                        continue  # every frame is decoded, but only some are sent on
                    ok, frame = cap.retrieve()
                    if not ok or frame is None:
                        continue
                    jpeg = encode(frame)
                    with self._lock:
                        self.jpeg, self.seq, self.error = jpeg, self.seq + 1, None
                    last = now
                    attempt = 0
            except Exception as exc:  # a bad frame must not end the reader
                log.warning("live %s: %s", self.name, exc)
                self.error = str(exc)
            finally:
                cap.release()
            if self._wanted():
                time.sleep(RETRY_S[min(attempt, len(RETRY_S) - 1)])
                attempt += 1
        log.info("live %s: nobody watching, stopped", self.name)


def part(jpeg: bytes) -> bytes:
    head = f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
    return head.encode() + jpeg + b"\r\n"


async def first_frame(cam: LiveCamera, timeout_s: float, poll_s: float = 0.05) -> bool:
    """Wait until the camera has a picture. False when it gives up or fails meanwhile."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cam.jpeg is not None:
            return True
        await asyncio.sleep(poll_s)
    return cam.jpeg is not None


async def mjpeg(
    cam: LiveCamera,
    disconnected: Callable[[], Any],
    poll_s: float = 0.02,
    stall_s: float = STALL_S,
) -> AsyncIterator[bytes]:
    """Each new frame as a multipart part, until the viewer leaves or the camera stalls."""
    seq = -1
    last_new = time.monotonic()
    try:
        while True:
            if await disconnected():
                return
            jpeg, now_seq = cam.jpeg, cam.seq
            if jpeg is not None and now_seq != seq:
                seq = now_seq
                last_new = time.monotonic()
                yield part(jpeg)
            elif time.monotonic() - last_new > stall_s:
                return
            else:
                await asyncio.sleep(poll_s)
    finally:
        cam.leave()


cameras = {name: LiveCamera(name) for name in RTSP_PATHS}
