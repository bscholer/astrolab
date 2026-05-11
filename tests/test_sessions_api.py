"""End-to-end tests for /api/sessions/{id} GET + PATCH (description path).

PATCH carries both description (free-text notes) and reassign fields
(target_id / new_target_name). These tests cover the description-only
slice; reassign coverage lives in test_api_sessions.py.

PATCH returns SessionPatchResponse: {session, deleted_target_ids}. A
description-only patch never deletes a target, so deleted_target_ids
is always empty here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.api import app, job_manager, project_manager
from server.cache import ContentCache
from server.catalog.db import default_db_path, open_db


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    db_path = default_db_path()
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    job_manager.reset_for_tests(db_path=db_path)
    project_manager.reset_for_tests(db_path=db_path)
    with TestClient(app) as c:
        yield c


def _seed_session(session_id: int = 1, target_name: str = "M 33") -> None:
    """Insert a target + session directly so we can exercise the API
    without running the scanner over fake FITS."""
    with open_db() as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO targets (id, name) VALUES (1, ?)",
            (target_name,),
        )
        conn.execute(
            """
            INSERT INTO sessions
            (id, scope_id, session_key, target_id, frame_count, failed_count)
            VALUES (?, 'dwarf3', ?, 1, 5, 0)
            """,
            (session_id, f"k{session_id}"),
        )


def test_get_session_returns_null_description_by_default(client: TestClient) -> None:
    _seed_session(1)
    r = client.get("/api/sessions/1")
    assert r.status_code == 200
    assert r.json()["description"] is None


def test_patch_description_round_trips(client: TestClient) -> None:
    _seed_session(1)
    r = client.patch(
        "/api/sessions/1",
        json={"description": "captured during full moon, expect background gradient"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["description"] == "captured during full moon, expect background gradient"
    assert body["deleted_target_ids"] == []

    # GET surfaces the saved note.
    get_body = client.get("/api/sessions/1").json()
    assert get_body["description"] == "captured during full moon, expect background gradient"


def test_patch_description_empty_string_clears(client: TestClient) -> None:
    _seed_session(1)
    client.patch("/api/sessions/1", json={"description": "hello"})
    r = client.patch("/api/sessions/1", json={"description": ""})
    assert r.json()["session"]["description"] is None
    r2 = client.patch("/api/sessions/1", json={"description": "hello"})
    assert r2.json()["session"]["description"] == "hello"
    r3 = client.patch("/api/sessions/1", json={"description": "   "})
    assert r3.json()["session"]["description"] is None


def test_patch_unknown_session_404(client: TestClient) -> None:
    r = client.patch("/api/sessions/9999", json={"description": "x"})
    assert r.status_code == 404
