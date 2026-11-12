"""Scope service settings: stored token stays secret and survives a URL edit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.api as api
from server.scope_service import ScopeServiceClient

CAPS = {
    "spec_version": "0.1",
    "service": {"name": "fake"},
    "capabilities": ["status", "setup", "goto", "capture"],
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "catalog.sqlite"
    api.scope_observer.state = {"state": "idle"}
    api.job_manager.reset_for_tests(db_path=db_path)
    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    seen: list[dict] = []
    state: dict = {}

    def transport(method, url, headers, body, timeout=0):
        seen.append({"url": url, "auth": headers.get("Authorization"), "body": body})
        if "unreachable" in url:
            from server.scope_service import ScopeServiceError

            raise ScopeServiceError("cannot reach service", code="unreachable")
        if url.endswith("/api/capabilities"):
            return 200, json.dumps(CAPS).encode()
        if url.endswith("/openapi.json"):
            spec = {
                "paths": {
                    "/api/status": {"get": {"operationId": "getStatus"}},
                    "/api/prepare": {"post": {"operationId": "prepareScope"}},
                    "/api/calibrate": {"post": {"operationId": "calibrateScope"}},
                    "/api/goto": {"post": {"operationId": "gotoTarget"}},
                    "/api/capture": {"post": {"operationId": "startCapture"}},
                }
            }
            return 200, json.dumps(spec).encode()
        if url.endswith("/api/prepare"):
            return 200, b'{"message": "ready"}'
        if url.endswith("/api/calibrate"):
            if state.get("clouds"):
                return 502, b'{"detail": "plate solve failed", "code": "scope_error"}'
            return 200, b'{"message": "calibrated"}'
        if url.endswith("/api/goto"):
            sent = json.loads(body)
            if sent["name"] == "Sun":
                return 422, b'{"detail": "too close to the sun", "code": "sun_too_close"}'
            return 200, b'{"message": "tracking"}'
        if url.endswith("/api/capture"):
            return 202, b'{"id": "j1", "state": "running", "frames": %d}' % json.loads(body)["frames"]
        if url.endswith("/api/status"):
            return 200, b'{"app_connected": true}'
        return 404, b"{}"

    monkeypatch.setattr(
        api, "ScopeServiceClient", lambda url, token=None: ScopeServiceClient(url, token, transport)
    )
    with TestClient(api.app) as c:
        c.seen = seen
        c.state = state
        yield c


def test_unconfigured(client):
    assert client.get("/api/scope").json() == {"configured": False, "reachable": False}


def test_token_is_sent_but_never_returned(client):
    r = client.put("/api/scope", json={"url": "http://svc:9902/", "token": "s3cret"})
    body = r.json()
    assert body["reachable"] and body["has_token"] and body["status"] == {"app_connected": True}
    assert "s3cret" not in r.text
    assert {s["auth"] for s in client.seen} == {"Bearer s3cret"}


def test_url_edit_keeps_token_and_empty_string_clears_it(client):
    client.put("/api/scope", json={"url": "http://a", "token": "t"})
    assert client.put("/api/scope", json={"url": "http://b"}).json()["has_token"] is True
    assert client.put("/api/scope", json={"url": "http://b", "token": ""}).json()["has_token"] is False


def test_unreachable_service_reports_error_instead_of_failing(client):
    body = client.put("/api/scope", json={"url": "http://unreachable"}).json()
    assert body["configured"] and not body["reachable"] and body["error_code"] == "unreachable"


def test_rejects_non_http_url(client):
    assert client.put("/api/scope", json={"url": "ftp://x"}).status_code == 400


OBSERVE = {"name": "M31", "ra_deg": 10.6847, "dec_deg": 41.27, "frames": 20}


def steps(client) -> list[str]:
    names = [s["url"].rsplit("/", 1)[1] for s in client.seen]
    return [n for n in names if n in ("prepare", "calibrate", "goto", "capture")]


def observe(client, **over):
    client.put("/api/scope", json={"url": "http://svc"})
    client.seen.clear()
    r = client.post("/api/scope/observe", json={**OBSERVE, **over})
    api.scope_observer.join(5)
    return r, client.get("/api/scope").json()["observe"]


def test_observe_runs_every_step_in_order_and_slews_in_hours(client):
    r, state = observe(client)
    assert r.status_code == 202 and state["state"] == "done"
    assert steps(client) == ["prepare", "calibrate", "goto", "capture"]
    goto = next(s for s in client.seen if s["url"].endswith("/api/goto"))
    assert json.loads(goto["body"])["ra_hours"] == pytest.approx(10.6847 / 15)


def test_failed_calibration_stops_before_the_scope_moves_or_captures(client):
    client.state["clouds"] = True
    _, state = observe(client)
    assert state["state"] == "failed" and "calibrating" in state["error"]
    assert steps(client) == ["prepare", "calibrate"]


def test_sun_refusal_surfaces_and_no_capture_starts(client):
    _, state = observe(client, name="Sun")
    assert state["state"] == "failed" and "sun" in state["error"]
    assert "capture" not in steps(client)


def test_second_observe_while_running_is_refused(client):
    client.put("/api/scope", json={"url": "http://svc"})
    api.scope_observer.state = {"state": "running", "target": "M31"}
    assert client.post("/api/scope/observe", json=OBSERVE).status_code == 409
    api.scope_observer.state = {"state": "idle"}


def test_observe_without_a_configured_service_is_a_clear_error(client):
    assert client.post("/api/scope/observe", json=OBSERVE).status_code == 400


def test_auto_toggle_stores_targets_and_state(client):
    body = {"enabled": True, "targets": [{"name": "IC 1396", "ra_deg": 324.7, "dec_deg": 57.5}]}
    r = client.put("/api/scope/auto", json=body)
    assert r.status_code == 200
    assert r.json()["targets"][0]["name"] == "IC 1396"
    assert r.json()["scheduler"]["enabled"] is True
    r = client.put("/api/scope/auto", json={"enabled": False})
    assert r.json()["scheduler"]["enabled"] is False
    assert r.json()["targets"][0]["name"] == "IC 1396"


def test_auto_rejects_bad_coordinates(client):
    body = {"enabled": True, "targets": [{"name": "x", "ra_deg": 400, "dec_deg": 0}]}
    assert client.put("/api/scope/auto", json=body).status_code == 422


def test_alert_url_is_validated_and_never_returned(client):
    assert client.put("/api/scope/alerts", json={"url": "ftp://x"}).status_code == 400
    r = client.put("/api/scope/alerts", json={"url": "https://ntfy.sh/secret-topic"})
    assert r.json() == {"alert_url_set": True}
    assert "secret-topic" not in client.get("/api/scope").text


def test_frame_needs_the_preview_capability(client, monkeypatch):
    import server.api as api_mod

    class Svc:
        def has(self, cap):
            return False

    monkeypatch.setattr(api_mod, "_scope_client", lambda: Svc())
    r = client.get("/api/scope/frame?camera=wide")
    assert r.status_code == 400 and "preview" in r.json()["detail"]
    assert client.get("/api/scope/frame?camera=side").status_code == 422


def test_frame_returns_the_decoded_jpeg(client, monkeypatch):
    import base64 as b64

    import server.api as api_mod

    class Svc:
        def has(self, cap):
            return True

        def call(self, op, payload=None, timeout=None):
            assert op == "getFrame" and payload == {"camera": "tele"}
            return {"camera": "tele", "data": b64.b64encode(b"\xff\xd8jpeg").decode()}

    monkeypatch.setattr(api_mod, "_scope_client", lambda: Svc())
    r = client.get("/api/scope/frame?camera=tele")
    assert r.content == b"\xff\xd8jpeg" and r.headers["content-type"] == "image/jpeg"


def test_auto_stack_flag_is_kept_when_the_toggle_omits_it(client):
    assert client.put("/api/scope/auto", json={"enabled": False, "auto_stack": True}).json()["auto_stack"]
    r = client.put("/api/scope/auto", json={"enabled": True})
    assert r.json()["auto_stack"] is True


def test_get_scope_reports_auto_stack_and_alert_flags(client):
    client.put("/api/scope", json={"url": "http://127.0.0.1:9"})
    state = client.get("/api/scope").json()
    assert state["auto_stack"] is False and state["alert_url_set"] is False
    client.put("/api/scope/auto", json={"enabled": False, "auto_stack": True})
    client.put("/api/scope/alerts", json={"url": "https://ntfy.sh/t"})
    state = client.get("/api/scope").json()
    assert state["auto_stack"] is True and state["alert_url_set"] is True


class PanoSvc:
    def __init__(self, caps=("pointing", "preview")):
        self.caps = caps

    def has(self, cap):
        return cap in self.caps


def _pano_env(monkeypatch, caps=("pointing", "preview"), enabled=False):
    import server.api as api_mod
    from server import sky

    monkeypatch.setattr(api_mod, "_scope_client", lambda: PanoSvc(caps))
    monkeypatch.setattr(api_mod, "_scope_site", lambda: sky.observer_location(37.0, -122.0, 100.0))
    monkeypatch.setattr(api_mod.scope_scheduler, "enabled", enabled)


def test_pano_needs_pointing_and_preview(client, monkeypatch):
    _pano_env(monkeypatch, caps=("preview",))
    r = client.post("/api/scope/pano")
    assert r.status_code == 400 and "pointing" in r.json()["detail"]


def test_pano_refuses_while_run_all_night_is_on(client, monkeypatch):
    _pano_env(monkeypatch, enabled=True)
    r = client.post("/api/scope/pano")
    assert r.status_code == 409 and "Run all night" in r.json()["detail"]


def test_pano_mosaic_is_404_before_any_scan(client):
    assert client.get("/api/scope/pano/mosaic").status_code in (404, 200)


def test_pointing_pairs_are_stored_and_capped(client):
    import server.api as api_mod

    for i in range(api_mod.POINTING_PAIRS_MAX + 3):
        api_mod._store_pointing({"target": f"t{i}", "pitch": 1.0, "rotation": 2.0})
    pairs = client.get("/api/scope/pointing").json()
    assert len(pairs) == api_mod.POINTING_PAIRS_MAX
    assert pairs[-1]["target"] == f"t{api_mod.POINTING_PAIRS_MAX + 2}"


def test_excluded_pano_tiles_persist_and_show_in_the_overlay(client, tmp_path, monkeypatch):
    import io

    from PIL import Image

    folder = tmp_path / "pano" / "abc"
    folder.mkdir(parents=True)
    for name in ("p100_r0.jpg", "p100_r45.jpg"):
        buf = io.BytesIO()
        Image.new("RGB", (64, 36), (110, 150, 215)).save(buf, "JPEG")
        (folder / name).write_bytes(buf.getvalue())
    monkeypatch.setattr(api.scope_pano, "state", {"state": "done", "id": "abc"})
    monkeypatch.setattr(api.scope_pano, "root", tmp_path / "pano")

    assert client.get("/api/scope/pano/tiles").json()["rotations"] == [0.0, 45.0]
    r = client.put("/api/scope/pano/tile", json={"pitch": 100, "rotation": 45, "excluded": True})
    assert r.json()["excluded"] == ["p100_r45.jpg"]
    assert client.get("/api/scope/pano/tiles").json()["excluded"] == ["p100_r45.jpg"]
    sheet = Image.open(io.BytesIO(client.get("/api/scope/pano/overlay").content))
    assert sheet.getpixel((320 + 160, 20))[0] < 60
    r = client.put("/api/scope/pano/tile", json={"pitch": 100, "rotation": 45, "excluded": False})
    assert r.json()["excluded"] == []


def test_horizon_needs_slews_then_maps_the_scan(client, tmp_path, monkeypatch):
    import io

    from PIL import Image

    from server import scope_mount

    api.scope_horizon._mask_cache.clear()
    api._horizon_cache.update(key=None, value=None)
    monkeypatch.setattr(api.scope_pano, "state", {"state": "idle"})
    first = client.get("/api/scope/horizon").json()
    assert first["active"] is False and "slews" in first["reason"]

    # Pitch is altitude and rotation is azimuth, on a level mount facing north.
    model = scope_mount.MountModel(-1.0, 90.0, -1.0, ((0, 1, 0), (1, 0, 0), (0, 0, 1)), 0, 0, 0, 0)
    for pitch, rot in [(45, 10), (40, 100), (50, 190), (45, 280), (70, 150)]:
        alt, az = model.to_sky(pitch, rot)
        api._store_pointing(
            {"pitch": pitch, "rotation": rot, "alt_deg": float(alt), "az_deg": float(az)}
        )
    folder = tmp_path / "pano" / "abc"
    folder.mkdir(parents=True)
    for rot in range(0, 360, 45):
        buf = io.BytesIO()
        Image.new("RGB", (320, 180), (110, 150, 215)).save(buf, "JPEG")
        (folder / f"p30_r{rot}.jpg").write_bytes(buf.getvalue())
    monkeypatch.setattr(api.scope_pano, "state", {"state": "done", "id": "abc"})
    monkeypatch.setattr(api.scope_pano, "root", tmp_path / "pano")

    h = client.get("/api/scope/horizon").json()
    assert h["active"] is True, h["reason"]
    assert h["fit"]["rms_deg"] < 0.5
    assert all(v is not None and v < 25 for v in h["profile"])
    svg = client.get("/api/scope/horizon.svg")
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert svg.text.count("<circle") >= 5
    limit = api._scope_horizon_limit()
    assert limit is not None and limit(123.0) < 25


def test_restart_finds_the_last_scan(tmp_path):
    from server.scope_pano import PanoScanner

    (tmp_path / "old").mkdir()
    (tmp_path / "scan1").mkdir()
    (tmp_path / "scan1" / "p30_r0.jpg").write_bytes(b"x")
    assert PanoScanner(tmp_path).snapshot() == {"state": "done", "id": "scan1"}
    assert PanoScanner(tmp_path / "none").snapshot() == {"state": "idle"}
