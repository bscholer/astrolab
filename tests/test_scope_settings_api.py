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
    "capabilities": ["status", "goto", "capture"],
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "catalog.sqlite"
    api.job_manager.reset_for_tests(db_path=db_path)
    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    seen: list[dict] = []

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
                    "/api/goto": {"post": {"operationId": "gotoTarget"}},
                    "/api/capture": {"post": {"operationId": "startCapture"}},
                }
            }
            return 200, json.dumps(spec).encode()
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


def test_observe_slews_in_hours_then_captures(client):
    client.put("/api/scope", json={"url": "http://svc"})
    client.seen.clear()
    r = client.post("/api/scope/observe", json=OBSERVE)
    assert r.status_code == 202 and r.json()["job"]["frames"] == 20
    calls = [s for s in client.seen if s["body"]]
    assert [c["url"].rsplit("/", 1)[1] for c in calls] == ["goto", "capture"]
    assert json.loads(calls[0]["body"])["ra_hours"] == pytest.approx(10.6847 / 15)


def test_observe_does_not_capture_when_goto_is_refused(client):
    client.put("/api/scope", json={"url": "http://svc"})
    client.seen.clear()
    r = client.post("/api/scope/observe", json={**OBSERVE, "name": "Sun"})
    assert r.status_code == 422 and "sun" in r.json()["detail"]
    assert not any(s["url"].endswith("/api/capture") for s in client.seen)


def test_observe_without_a_configured_service_is_a_clear_error(client):
    assert client.post("/api/scope/observe", json=OBSERVE).status_code == 400
