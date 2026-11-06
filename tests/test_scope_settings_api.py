"""Scope service settings: stored token stays secret and survives a URL edit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.api as api
from server.scope_service import ScopeServiceClient

CAPS = {"spec_version": "0.1", "service": {"name": "fake"}, "capabilities": ["status", "goto"]}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "catalog.sqlite"
    api.job_manager.reset_for_tests(db_path=db_path)
    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    seen: list[dict] = []

    def transport(method, url, headers, body):
        seen.append({"url": url, "auth": headers.get("Authorization")})
        if "unreachable" in url:
            from server.scope_service import ScopeServiceError

            raise ScopeServiceError("cannot reach service", code="unreachable")
        if url.endswith("/api/capabilities"):
            return 200, json.dumps(CAPS).encode()
        if url.endswith("/openapi.json"):
            spec = {"paths": {"/api/status": {"get": {"operationId": "getStatus"}}}}
            return 200, json.dumps(spec).encode()
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
