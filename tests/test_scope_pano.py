"""Pano scan: refuses in daylight, finds the rotation stops, plans rings between them, keeps
what the scope reports, and keeps going past a frame that fails."""

from __future__ import annotations

import base64
import io
import json
import time
from datetime import UTC, datetime

import pytest
from PIL import Image

from server import scope_pano, sky
from server.scope_pano import PanoRefused, PanoScanner, plan_stops

LOC = sky.observer_location(37.0, -122.0, 100.0)
NIGHT = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
NOON = datetime(2026, 11, 7, 20, 0, tzinfo=UTC)


def _jpeg(color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buf, "JPEG")
    return buf.getvalue()


class Scope:
    """A relay whose rotation motor stops at 12.3 and 303.2, like the real one."""

    def __init__(self, fail_at=None, silent_status=False):
        self.pitch, self.rotation = 172.5, 158.0
        self.points = []
        self.fail_at = fail_at
        self.silent_status = silent_status

    def call(self, op, payload=None, timeout=None):
        if op == "pointScope":
            self.points.append((payload["pitch"], payload["rotation"]))
            if self.fail_at is not None and abs(payload["rotation"] - self.fail_at) < 0.01:
                raise RuntimeError("motor refused")
            self.pitch = payload["pitch"]
            self.rotation = min(303.2, max(12.3, payload["rotation"]))
            return {"message": "pointed"}
        if op == "getStatus":
            if self.silent_status and len(self.points) > 2:
                return {"pitch": None, "rotation": None}
            return {"pitch": self.pitch, "rotation": self.rotation}
        return {"camera": "wide", "data": base64.b64encode(_jpeg()).decode()}


def _wait(scanner):
    for _ in range(400):
        if scanner.snapshot()["state"] not in ("running",):
            return scanner.snapshot()
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(scope_pano, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(scope_pano, "run_stitch", lambda folder, lat: {"alt_top": 90.0})
    monkeypatch.setattr(scope_pano.pano_sky, "detect", lambda folder, top: None)
    monkeypatch.setattr(scope_pano.pano_sky, "save_horizon", lambda folder, result: {})


def test_refuses_while_the_sun_is_up(tmp_path):
    with pytest.raises(PanoRefused, match="Sun"):
        PanoScanner(tmp_path).start(Scope(), LOC, when=NOON)


def test_a_covered_tele_lens_allows_daylight(tmp_path):
    scanner = PanoScanner(tmp_path)
    scanner.start(Scope(), LOC, when=NOON, tele_covered=True, settle_s=0)
    assert _wait(scanner)["state"] == "done"


def test_plan_covers_each_ring_from_stop_to_stop_and_snakes():
    stops = plan_stops(12.5, 303.0)
    rings: dict[float, list[float]] = {}
    for pitch, rot in stops:
        rings.setdefault(pitch, []).append(rot)
    pitches = sorted(rings, reverse=True)
    assert pitches[0] == 175.0 and pitches[-1] == 50.0
    for k, pitch in enumerate(pitches):
        rots = rings[pitch]
        assert min(rots) == 12.5 and max(rots) == 303.0
        assert rots == sorted(rots, reverse=bool(k % 2))
    # Frames are 46 x 27 degrees: neighbours must overlap.
    assert all(b - a < 27 * 0.9 for a, b in zip(pitches[1:], pitches[:-1], strict=True))
    square = rings[min(pitches, key=lambda p: abs(p - 81))]
    assert max(abs(a - b) for a, b in zip(square[1:], square[:-1], strict=True)) < 46 * 0.75


def test_scan_finds_the_stops_and_keeps_what_the_scope_reports(tmp_path):
    scope = Scope()
    scanner = PanoScanner(tmp_path)
    scan = scanner.start(scope, LOC, when=NIGHT, settle_s=0)
    snap = _wait(scanner)
    assert snap["state"] == "done" and snap["failed"] == 0
    assert scope.points[:2] == [(175.0, 0.0), (175.0, 360.0)]
    assert snap["rotation_limits"] == [12.3, 303.2]
    manifest = json.loads((tmp_path / scan / "manifest.json").read_text())
    assert len(manifest["tiles"]) == snap["total"] == len(plan_stops(12.3, 303.2))
    assert all(12.3 <= t["rotation"] <= 303.2 for t in manifest["tiles"])
    assert all(t["reported"] for t in manifest["tiles"])


def test_asked_angles_stand_in_when_the_scope_reports_none(tmp_path):
    # Readings for the two probe moves, then the relay has none, as it sometimes does.
    scanner = PanoScanner(tmp_path)
    scan = scanner.start(Scope(silent_status=True), LOC, when=NIGHT, settle_s=0)
    assert _wait(scanner)["state"] == "done"
    tiles = json.loads((tmp_path / scan / "manifest.json").read_text())["tiles"]
    assert tiles and not any(t["reported"] for t in tiles)
    assert all(t["rotation"] == t["asked_rotation"] for t in tiles)


def test_no_readings_at_all_fails_before_moving_on(tmp_path):
    scope = Scope()
    scope.call = lambda op, payload=None, timeout=None: (
        {"pitch": None, "rotation": None} if op == "getStatus" else {"message": "pointed"}
    )
    scanner = PanoScanner(tmp_path)
    scanner.start(scope, LOC, when=NIGHT, settle_s=0)
    snap = _wait(scanner)
    assert snap["state"] == "failed" and "rotation motor" in snap["error"]


def test_a_failed_frame_is_counted_and_the_scan_goes_on(tmp_path):
    stops = plan_stops(12.3, 303.2)
    scope = Scope(fail_at=stops[3][1])
    scanner = PanoScanner(tmp_path)
    scan = scanner.start(scope, LOC, when=NIGHT, settle_s=0)
    snap = _wait(scanner)
    failed = sum(1 for _, r in stops if abs(r - stops[3][1]) < 0.01)
    assert snap["state"] == "done" and snap["failed"] == failed
    tiles = json.loads((tmp_path / scan / "manifest.json").read_text())["tiles"]
    assert len(tiles) == len(stops) - failed


def test_restart_finds_the_last_scan(tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "scan1").mkdir()
    (tmp_path / "scan1" / "manifest.json").write_text('{"tiles": []}')
    (tmp_path / "scan1" / "pano.json").write_text('{"alt_top": 90}')
    snap = PanoScanner(tmp_path).snapshot()
    assert snap["state"] == "done" and snap["id"] == "scan1"
    assert snap["result"] == {"alt_top": 90}
    assert PanoScanner(tmp_path / "none").snapshot() == {"state": "idle"}


def test_a_stitch_that_uses_too_much_memory_is_stopped(monkeypatch):
    import subprocess
    import sys

    monkeypatch.setattr(scope_pano, "STITCH_MEMORY_BYTES", 100 * 1024**2)
    monkeypatch.setattr(scope_pano, "WATCH_S", 0.05)
    hog = "import time; x = bytearray(400 * 1024**2); time.sleep(30)"
    proc = subprocess.Popen([sys.executable, "-c", hog])
    assert "memory" in scope_pano._watch(proc)
    assert proc.returncode is not None


def test_a_stitch_within_its_limits_is_left_alone():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "x = bytearray(10 * 1024**2)"])
    assert scope_pano._watch(proc) is None and proc.returncode == 0
