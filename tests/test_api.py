"""HTTP API tests using FastAPI TestClient.

The conftest autouse fixture isolates ASTROLAB_HOME, so the API hits a
disposable SQLite per test. We seed the catalog by running a real scan
against a synthetic Dwarf 3 tree, then exercise the endpoints.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.api import app
from server.catalog.scanner import scan

from ._fits_fixtures import DEFAULT_DARK_HEADER, DEFAULT_LIGHT_HEADER, write_fits


def _seed_tree(root: Path) -> None:
    light = root / "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    write_fits(
        light / "M 33_30s60_Astro_20251021-221929504_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )
    write_fits(
        light / "failed_M 33_30s60_Astro_20251021-222229500_24C.fits",
        headers={**DEFAULT_LIGHT_HEADER, "OBJECT": "M 33"},
    )
    raw_dark = root / "DWARF_DARK" / "tele_exp_30_gain_60_bin_1_2025-10-21-00-37-45-393"
    write_fits(
        raw_dark / "raw_30s_60_0000_20251021-003814624_22C.fits",
        headers=DEFAULT_DARK_HEADER,
    )
    cali = root / "CALI_FRAME" / "dark" / "cam_0"
    write_fits(cali / "dark_exp_30.000000_gain_60_bin_1_24C_stack_10.fits")


@pytest.fixture
def client(tmp_path: Path, astrolab_home: Path) -> TestClient:
    captures = tmp_path / "captures"
    _seed_tree(captures)
    scan(captures)
    return TestClient(app)


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_list_targets(client: TestClient) -> None:
    r = client.get("/api/targets")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    target = body[0]
    assert target["name"] == "M 33"
    assert target["session_count"] == 1
    assert target["frame_count"] == 2
    assert target["failed_count"] == 1


def test_target_detail(client: TestClient) -> None:
    r = client.get("/api/targets")
    target_id = r.json()[0]["id"]
    r = client.get(f"/api/targets/{target_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "M 33"
    assert len(body["sessions"]) == 1
    session = body["sessions"][0]
    assert session["frame_count"] == 2
    assert session["failed_count"] == 1
    assert session["target_name"] == "M 33"

    cal_by_kind = {c["kind"]: c for c in session["calibration"]}
    assert cal_by_kind["dark"]["quality"] == "exact"
    assert cal_by_kind["dark"]["master_id"] is not None
    assert cal_by_kind["flat"]["quality"] == "none"
    assert cal_by_kind["bias"]["quality"] == "none"


def test_target_404(client: TestClient) -> None:
    r = client.get("/api/targets/9999")
    assert r.status_code == 404


def test_target_storage_and_integration(client: TestClient) -> None:
    """The library exposes per-target bytes_on_disk + integration_seconds.
    Two seeded LIGHT frames, one failed, exposure 30s; useful integration
    is 1 frame * 30s = 30s, and bytes is the on-disk size of both
    (failed frames still take up space)."""
    body = client.get("/api/targets").json()
    assert len(body) == 1
    t = body[0]
    # 2 frames at exptime 30s, failed_count=1 -> useful = 30s.
    assert t["integration_seconds"] == 30.0
    # Synthetic FITS files are non-empty (a few KB each).
    assert t["bytes_on_disk"] > 0


def test_session_storage_and_integration(client: TestClient) -> None:
    target_id = client.get("/api/targets").json()[0]["id"]
    sessions = client.get(f"/api/targets/{target_id}").json()["sessions"]
    s = sessions[0]
    assert s["integration_seconds"] == 30.0
    assert s["bytes_on_disk"] > 0


def test_session_detail(client: TestClient) -> None:
    r = client.get("/api/targets")
    target_id = r.json()[0]["id"]
    sessions = client.get(f"/api/targets/{target_id}").json()["sessions"]
    sid = sessions[0]["id"]
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid
    assert body["target_name"] == "M 33"


def test_session_404(client: TestClient) -> None:
    r = client.get("/api/sessions/9999")
    assert r.status_code == 404


def test_list_masters(client: TestClient) -> None:
    r = client.get("/api/masters")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["kind"] == "dark"
    assert body[0]["camera"] == "TELE"


def test_scan_endpoint(tmp_path: Path, astrolab_home: Path) -> None:
    """POST /api/scan returns 202 and runs the scan in a background thread;
    poll /api/scan/status until idle, then assert the catalog populated."""
    captures = tmp_path / "captures2"
    _seed_tree(captures)
    client = TestClient(app)
    r = client.post("/api/scan", json={"root": str(captures)})
    assert r.status_code == 202
    assert r.json() == {"status": "started"}

    # Poll status until the background scan finishes; bail out at 10s so a
    # genuinely broken scan doesn't hang the suite.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        s = client.get("/api/scan/status").json()
        if not s["running"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("scan never finished within 10s")

    snap = client.get("/api/scan/status").json()
    assert snap["error"] is None, snap
    stats = snap["last_stats"]
    assert stats is not None
    assert stats["discovered"] >= 4
    assert stats["masters_inserted"] >= 1


def test_dark_preview_empty_when_no_session_ids(client: TestClient) -> None:
    """Empty bundle = empty bins, empty summary. Useful sentinel so the UI
    can render the modal even before the user has picked anything."""
    r = client.get("/api/sessions/dark-preview")
    assert r.status_code == 200
    body = r.json()
    assert body["bins"] == []
    assert body["summary"] == {
        "total_frames": 0,
        "matched_frames": 0,
        "unmatched_frames": 0,
        "fallback_frames": 0,
        "darks_used": 0,
    }


def test_dark_preview_matches_seeded_bundle(client: TestClient) -> None:
    """The seeded tree has one 30s light at 24C and one factory dark
    at exptime=30, ccd_temp=24 — they should match into a single bin
    with the master picked from the catalog."""
    # No /api/sessions list endpoint; reach a session id via the target.
    targets = client.get("/api/targets").json()
    assert targets, "fixture should seed at least one target"
    detail = client.get(f"/api/targets/{targets[0]['id']}").json()
    assert detail["sessions"], "target should have at least one session"
    sid = detail["sessions"][0]["id"]
    r = client.get(f"/api/sessions/dark-preview?session_id={sid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["bins"]) == 1, body
    bin0 = body["bins"][0]
    assert bin0["exptime"] == 30.0
    assert bin0["frame_count"] >= 1
    assert bin0["master_id"] is not None
    assert bin0["master_name"]
    assert bin0["master_name"].endswith(".fits")
    assert bin0["fallback"] is False
    assert body["summary"]["unmatched_frames"] == 0
    assert body["summary"]["darks_used"] == 1


def test_scan_endpoint_rejects_missing_root(astrolab_home: Path) -> None:
    client = TestClient(app)
    r = client.post(
        "/api/scan", json={"root": "/nonexistent-astrolab"}
    )
    assert r.status_code == 400
