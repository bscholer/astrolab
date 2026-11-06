"""Decorator behavior: error mapping, auth, OpenAPI shape, MCP calls, spec drift."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from scripts.export_spec import DOCS, generated
from server import endpoints
from server.endpoints import (
    AuthMode,
    StatusHint,
    ToolAnnotations,
    http_endpoint,
    mcp_tool,
    mount_endpoints,
)


class MissingError(Exception):
    pass


class EchoIn(BaseModel):
    name: str
    times: int = 1


class EchoOut(BaseModel):
    text: str


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(endpoints, "_HTTP", [])
    monkeypatch.setattr(endpoints, "_TOOLS", {})

    @mcp_tool(
        name="echo",
        title="Echo",
        description="Repeat a name.",
        annotations=ToolAnnotations(read_only_hint=True),
        error_map={MissingError: "No such name."},
    )
    @http_endpoint(
        path="/t/echo",
        operation_id="echo",
        method="GET",
        payload_model=EchoIn,
        response_model=EchoOut,
        description="Repeat a name.",
        tags=["T"],
        error_map={MissingError: StatusHint(404, "name_missing")},
    )
    def echo(p: EchoIn) -> EchoOut:
        if p.name == "nobody":
            raise MissingError("nobody home")
        return EchoOut(text=p.name * p.times)

    @mcp_tool(
        name="secret", title="Secret", description="Needs a token.", authentication=AuthMode.TOKEN
    )
    @http_endpoint(
        path="/t/secret",
        operation_id="secret",
        method="POST",
        payload_model=EchoIn,
        response_model=EchoOut,
        description="Needs a token.",
        tags=["T"],
        authentication=AuthMode.TOKEN,
    )
    def secret(p: EchoIn) -> EchoOut:
        return EchoOut(text="ok")

    app = FastAPI()
    mount_endpoints(app)
    return TestClient(app)


def test_get_payload_comes_from_query_and_error_maps_to_status(client: TestClient) -> None:
    assert client.get("/t/echo", params={"name": "ab", "times": 2}).json() == {"text": "abab"}
    r = client.get("/t/echo", params={"name": "nobody"})
    assert r.status_code == 404
    assert r.json() == {"detail": "nobody home", "code": "name_missing"}


def test_invalid_payload_uses_the_error_shape(client: TestClient) -> None:
    r = client.get("/t/echo", params={"times": "x"})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "invalid_request"
    assert "name" in body["detail"] and "times" in body["detail"]


def test_openapi_lists_operation_id_and_documented_errors(client: TestClient) -> None:
    op = client.get("/openapi.json").json()["paths"]["/t/echo"]["get"]
    assert op["operationId"] == "echo"
    assert op["responses"]["404"]["description"] == "name_missing"


def test_token_endpoint_fails_closed_until_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"name": "x"}
    monkeypatch.delenv(endpoints.TOKEN_ENV, raising=False)
    assert client.post("/t/secret", json=body).status_code == 503
    monkeypatch.setenv(endpoints.TOKEN_ENV, "s3cret")
    assert client.post("/t/secret", json=body).status_code == 401
    assert (
        client.post("/t/secret", json=body, headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    assert (
        client.post("/t/secret", json=body, headers={"Authorization": "Bearer s3cret"}).status_code
        == 200
    )


def _rpc(
    client: TestClient, method: str, params: dict | None = None, headers: dict | None = None
) -> dict:
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers,
    )
    return r.json()["result"]


def test_mcp_tool_call_maps_errors_to_friendly_text(client: TestClient) -> None:
    ok = _rpc(client, "tools/call", {"name": "echo", "arguments": {"name": "a", "times": 3}})
    assert ok["isError"] is False and ok["structuredContent"] == {"text": "aaa"}
    bad = _rpc(client, "tools/call", {"name": "echo", "arguments": {"name": "nobody"}})
    assert bad["isError"] is True and bad["content"][0]["text"] == "No such name."
    invalid = _rpc(client, "tools/call", {"name": "echo", "arguments": {"times": "x"}})
    assert invalid["isError"] is True


def test_mcp_manifest_schema_and_annotations(client: TestClient) -> None:
    tools = {t["name"]: t for t in _rpc(client, "tools/list")["tools"]}
    assert tools["echo"]["inputSchema"]["required"] == ["name"]
    assert tools["echo"]["annotations"] == {"readOnlyHint": True}


def test_mcp_token_tool_requires_bearer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(endpoints.TOKEN_ENV, "s3cret")
    args = {"name": "secret", "arguments": {"name": "x"}}
    assert _rpc(client, "tools/call", args)["isError"] is True
    assert _rpc(client, "tools/call", args, {"Authorization": "Bearer s3cret"})["isError"] is False


def test_duplicate_operation_id_is_rejected(client: TestClient) -> None:
    with pytest.raises(ValueError):
        http_endpoint(path="/x", operation_id="echo", method="GET", description="d", tags=[])(
            lambda: None
        )


def test_committed_spec_matches_code() -> None:
    for name, text in generated().items():
        assert (DOCS / name).read_text() == text, f"{name} is stale: python -m scripts.export_spec"
