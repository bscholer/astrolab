"""Pano: refuses in daylight, tiles frames in a fixed grid, and keeps going past a failed tile."""

from __future__ import annotations

import base64
import io
from datetime import UTC, datetime

import pytest
from PIL import Image

from server import sky
from server import scope_pano
from server.scope_pano import PanoRefused, PanoScanner, build_mosaic, tile_name

LOC = sky.observer_location(37.0, -122.0, 100.0)
NIGHT = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
NOON = datetime(2026, 11, 7, 20, 0, tzinfo=UTC)


def _jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buf, "JPEG")
    return buf.getvalue()


class Fake:
    def __init__(self, fail_at=None):
        self.points = []
        self.fail_at = fail_at

    def call(self, op, payload=None, timeout=None):
        if op == "pointScope":
            self.points.append((payload["pitch"], payload["rotation"]))
            if self.fail_at == payload["rotation"]:
                raise RuntimeError("motor refused")
            return {}
        return {"camera": "wide", "data": base64.b64encode(_jpeg((200, 30, 30))).decode()}


def test_refuses_while_the_sun_is_up(tmp_path):
    with pytest.raises(PanoRefused, match="Sun"):
        PanoScanner(tmp_path).start(Fake(), LOC, when=NOON)


def test_scan_visits_every_stop_and_builds_the_mosaic(tmp_path):
    scanner = PanoScanner(tmp_path)
    client = Fake()
    scanner.start(client, LOC, pitches=(40.0, 100.0), rotations=(0.0, 90.0), settle_s=0, when=NIGHT)
    for _ in range(100):
        if scanner.snapshot()["state"] != "running":
            break
        import time

        time.sleep(0.05)
    snap = scanner.snapshot()
    assert snap["state"] == "done" and snap["done"] == 4
    assert client.points == [(40.0, 0.0), (40.0, 90.0), (100.0, 0.0), (100.0, 90.0)]
    assert Image.open(scanner.mosaic_path()).size == (640, 360)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(scope_pano, "RETRY_WAIT_S", 0)


class Flaky(Fake):
    def __init__(self):
        super().__init__()
        self.refused = False

    def call(self, op, payload=None, timeout=None):
        if op == "pointScope" and not self.refused:
            self.refused = True
            raise RuntimeError("another tool call is running")
        return super().call(op, payload, timeout)


def test_a_busy_service_is_retried_once(tmp_path):
    scanner = PanoScanner(tmp_path)
    scanner.start(Flaky(), LOC, pitches=(40.0,), rotations=(0.0,), settle_s=0, when=NIGHT)
    import time

    for _ in range(100):
        if scanner.snapshot()["state"] != "running":
            break
        time.sleep(0.05)
    assert scanner.snapshot()["failed"] == 0
    assert (tmp_path / scanner.snapshot()["id"] / tile_name(40.0, 0.0)).is_file()


def test_failed_tile_is_counted_and_left_black(tmp_path):
    scanner = PanoScanner(tmp_path)
    scanner.start(
        Fake(fail_at=90.0), LOC, pitches=(40.0,), rotations=(0.0, 90.0), settle_s=0, when=NIGHT
    )
    import time

    for _ in range(100):
        if scanner.snapshot()["state"] != "running":
            break
        time.sleep(0.05)
    snap = scanner.snapshot()
    assert snap["state"] == "done" and snap["failed"] == 1
    sheet = Image.open(scanner.mosaic_path())
    assert sheet.getpixel((480, 90)) == (0, 0, 0)
    assert sheet.getpixel((160, 90))[0] > 100


def test_mosaic_puts_high_pitch_on_top(tmp_path):
    (tmp_path / tile_name(40.0, 0.0)).write_bytes(_jpeg((255, 0, 0)))
    (tmp_path / tile_name(160.0, 0.0)).write_bytes(_jpeg((0, 0, 255)))
    sheet = Image.open(build_mosaic(tmp_path, (40.0, 160.0), (0.0,)))
    assert sheet.getpixel((10, 10))[2] > 150
    assert sheet.getpixel((10, 250))[0] > 150
