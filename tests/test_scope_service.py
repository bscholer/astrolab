"""Scope service client against a fake service that uses no scope-specific terms."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from server.scope_service import ScopeServiceClient, ScopeServiceError


class Target(BaseModel):
    name: str
    ra_hours: float


def fake_service(spec_version: str = "0.1", token: str | None = None) -> FastAPI:
    app = FastAPI()
    seen: list[dict] = []
    app.state.seen = seen

    def guard(auth: str | None) -> None:
        if token and auth != f"Bearer {token}":
            raise HTTPException(401, "unauthorized")

    @app.get("/api/capabilities")
    def caps(authorization: str | None = Header(None)) -> dict:
        guard(authorization)
        return {"spec_version": spec_version, "service": {"name": "fake"}, "capabilities": ["goto"]}

    @app.get("/v2/where", operation_id="getWhere")
    def where(wide: bool = False) -> dict:
        seen.append({"wide": wide})
        return {"wide": wide}

    @app.post("/v2/slew", operation_id="gotoTarget")
    def slew(t: Target) -> dict:
        seen.append(t.model_dump())
        if t.ra_hours > 24:
            raise HTTPException(status_code=422, detail="ra out of range")
        return {"message": f"tracking {t.name}"}

    return app


def client_for(app: FastAPI, **kwargs) -> ScopeServiceClient:
    http = TestClient(app)

    def transport(method, url, headers, body):
        r = http.request(method, url.replace("http://svc", ""), headers=headers, content=body)
        return r.status_code, r.content

    return ScopeServiceClient("http://svc", transport=transport, **kwargs)


def test_paths_come_from_the_service_openapi_not_the_client() -> None:
    app = fake_service()
    c = client_for(app)
    assert c.call("gotoTarget", {"name": "M31", "ra_hours": 0.7}) == {"message": "tracking M31"}
    assert c.call("getWhere", {"wide": True}) == {"wide": True}
    assert app.state.seen == [{"name": "M31", "ra_hours": 0.7}, {"wide": True}]


def test_capability_gating_and_unknown_operation() -> None:
    c = client_for(fake_service())
    assert c.has("goto") and not c.has("darks")
    with pytest.raises(ScopeServiceError) as err:
        c.call("startDark", {})
    assert err.value.code == "unsupported"


def test_other_major_version_is_refused() -> None:
    with pytest.raises(ScopeServiceError) as err:
        client_for(fake_service(spec_version="1.0")).info()
    assert err.value.code == "unsupported_version"


def test_minor_version_bump_is_accepted() -> None:
    assert client_for(fake_service(spec_version="0.9")).info()["service"]["name"] == "fake"


def test_service_error_text_and_status_reach_the_caller() -> None:
    c = client_for(fake_service())
    with pytest.raises(ScopeServiceError) as err:
        c.call("gotoTarget", {"name": "bad", "ra_hours": 30})
    assert (err.value.status, str(err.value)) == (422, "ra out of range")


def test_token_is_sent_as_a_bearer_header() -> None:
    app = fake_service(token="s3")
    with pytest.raises(ScopeServiceError) as err:
        client_for(app).info()
    assert err.value.status == 401
    assert client_for(app, token="s3").info()["capabilities"] == ["goto"]
