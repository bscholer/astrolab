"""Scope service settings: stored token stays secret and survives a URL edit."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
    api._frame_last.clear()
    api.scope_skymap._cells = None
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
    api._frame_last.clear()
    api.scope_skymap._cells = None


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


def test_pano_image_is_404_before_any_scan(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api.scope_pano, "state", {"state": "idle"})
    assert client.get("/api/scope/pano/image").status_code == 404
    assert client.get("/api/scope/horizon").status_code == 404


def test_pointing_pairs_are_stored_and_capped(client):
    import server.api as api_mod

    for i in range(api_mod.POINTING_PAIRS_MAX + 3):
        api_mod._store_pointing({"target": f"t{i}", "pitch": 1.0, "rotation": 2.0})
    pairs = client.get("/api/scope/pointing").json()
    assert len(pairs) == api_mod.POINTING_PAIRS_MAX
    assert pairs[-1]["target"] == f"t{api_mod.POINTING_PAIRS_MAX + 2}"


def _pano_scan(tmp_path, monkeypatch, compass: bool):
    """A finished scan: a panorama whose top half is blue sky and bottom half a wall."""
    import cv2
    import numpy as np

    folder = tmp_path / "pano" / "abc"
    folder.mkdir(parents=True)
    rng = np.random.default_rng(0)
    pano = np.zeros((1024, 4096, 3), np.uint8)
    pano[:512] = (215, 150, 110)  # BGR blue
    pano[512:] = rng.integers(20, 120, (512, 4096, 3), dtype=np.uint8)
    cv2.imwrite(str(folder / "pano.jpg"), pano)
    result = {"alt_top": 45.0, "alt_bottom": -45.0, "compass": compass}
    (folder / "pano.json").write_text(json.dumps(result))
    (folder / "manifest.json").write_text('{"tiles": []}')
    monkeypatch.setattr(api.scope_pano, "root", tmp_path / "pano")
    monkeypatch.setattr(api.scope_pano, "state", {"state": "done", "id": "abc", "result": result})
    return folder


def test_sky_is_found_and_the_horizon_follows_hand_fixes(client, tmp_path, monkeypatch):
    import cv2
    import numpy as np

    _pano_scan(tmp_path, monkeypatch, compass=True)
    horizon = client.post("/api/scope/pano/sky/detect").json()["horizon"]
    # The wall's top is at altitude 0 all the way round.
    assert all(abs(a) < 1.5 for a in horizon["alt"])
    auto = cv2.imdecode(
        np.frombuffer(client.get("/api/scope/pano/sky?layer=auto").content, np.uint8), 0
    )
    assert auto.shape == (1024, 2048)
    assert (auto[262:400] == 255).mean() > 0.95  # sky, altitude 44 to 20 (row 256 is 45)
    assert (auto[600:700] == 0).mean() > 0.95  # wall
    assert (auto[:250] == 128).all()  # above what the scan saw
    # Paint a tree from azimuth 90 to 100, up to altitude 30.
    edits = np.zeros_like(auto)
    edits[int((90 - 30) / 180 * 1024) : 512, 512:569] = 2
    ok, png = cv2.imencode(".png", edits)
    r = client.put("/api/scope/pano/sky", content=png.tobytes())
    assert r.status_code == 200
    alt = r.json()["horizon"]["alt"]
    assert 29 < alt[95] < 31 and abs(alt[120]) < 1.5
    assert client.get("/api/scope/horizon").json()["alt"][95] == alt[95]
    back = cv2.imdecode(np.frombuffer(client.get("/api/scope/pano/sky?layer=edits").content, np.uint8), 0)
    assert (back == edits).all()


def test_a_mask_of_the_wrong_size_is_refused(client, tmp_path, monkeypatch):
    import cv2
    import numpy as np

    _pano_scan(tmp_path, monkeypatch, compass=True)
    client.post("/api/scope/pano/sky/detect")
    ok, png = cv2.imencode(".png", np.zeros((10, 10), np.uint8))
    assert client.put("/api/scope/pano/sky", content=png.tobytes()).status_code == 400


def test_the_atlas_horizon_needs_a_compass(client, tmp_path, monkeypatch):
    _pano_scan(tmp_path, monkeypatch, compass=False)
    client.post("/api/scope/pano/sky/detect")
    assert client.get("/api/scope/horizon").status_code == 404
    assert client.get("/api/scope/horizon?compass_only=false").status_code == 200


def test_learned_patches_carry_their_verdict(client):
    api.scope_skymap.clear()
    for _ in range(4):
        api.scope_skymap.record(50.0, 120.0, good=1, failed=0, at=datetime.now(UTC))
    patches = client.get("/api/scope/skymap").json()["patches"]
    assert patches["50/120"]["verdict"] == "clear"


def test_frame_requests_share_one_grab(client, monkeypatch):
    import base64 as b64

    calls = []

    class Svc:
        def has(self, cap):
            return True

        def call(self, op, payload=None, timeout=None):
            calls.append(op)
            return {"data": b64.b64encode(b"jpeg").decode()}

    monkeypatch.setattr(api, "_scope_client", lambda: Svc())
    assert client.get("/api/scope/frame?camera=wide").content == b"jpeg"
    assert client.get("/api/scope/frame?camera=wide").content == b"jpeg"
    assert calls == ["getFrame"]
    client.get("/api/scope/frame?camera=tele")
    assert calls == ["getFrame", "getFrame"]


def test_skymap_reports_learned_patches_and_forgets(client):
    from datetime import UTC, datetime

    at = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    api.scope_skymap.record(40.0, 120.0, 5, 0, at)
    body = client.get("/api/scope/skymap").json()
    assert body["cells"] == {"clear": 1, "blocked": 0, "unknown": 0}
    assert "40/120" in body["patches"]
    svg = client.get("/api/scope/skymap.svg")
    assert svg.headers["content-type"].startswith("image/svg+xml") and "<circle" in svg.text
    assert client.delete("/api/scope/skymap").json()["cells"]["clear"] == 0


def test_scope_url_and_site_seed_from_the_environment_once(client, monkeypatch):
    from server.storage import SETTING_SCOPE_URL, SETTING_SITE_LATITUDE, get_setting

    monkeypatch.setenv("ASTROLAB_SCOPE_URL_DEFAULT", "http://fakescope:9902")
    monkeypatch.setenv("ASTROLAB_SITE_DEFAULT", "44.0,-121.3,1100")
    api._bootstrap_scope_settings()
    db = api.job_manager.db_path
    assert get_setting(SETTING_SCOPE_URL, None, db_path=db) == "http://fakescope:9902"
    assert get_setting(SETTING_SITE_LATITUDE, None, db_path=db) == 44.0
    monkeypatch.setenv("ASTROLAB_SCOPE_URL_DEFAULT", "http://other")
    api._bootstrap_scope_settings()
    assert get_setting(SETTING_SCOPE_URL, None, db_path=db) == "http://fakescope:9902"


def test_a_restitch_gives_the_image_a_new_version(client, tmp_path, monkeypatch):
    import os

    folder = _pano_scan(tmp_path, monkeypatch, compass=True)
    first = client.get("/api/scope/pano").json()["image_version"]
    stamp = (folder / "pano.jpg").stat().st_mtime_ns + 5_000_000_000
    os.utime(folder / "pano.jpg", ns=(stamp, stamp))  # written again by a restitch
    assert client.get("/api/scope/pano").json()["image_version"] > first
