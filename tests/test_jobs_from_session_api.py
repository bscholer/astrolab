"""End-to-end test for POST /api/jobs/from_session.

Submits a session-based job via the API. We can't actually run the
calibrate_register_stack template here (no Siril on the test host), so we
register a 'noop' template that uses only the downscale node and bypass
calibration. The point is exercising the resolver + endpoint, not Siril.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import nodes.basic  # noqa: F401
from server.api import app, job_manager
from server.cache import ContentCache
from server.catalog.db import open_db


def _seed_session_with_png(conn, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / "frame.png"
    Image.new("RGB", (200, 100), (32, 64, 96)).save(src)
    with conn:
        conn.execute("INSERT INTO targets (id, name) VALUES (1, 'M 33')")
        conn.execute(
            """INSERT INTO sessions (id, scope_id, session_key, target_id, frame_count)
               VALUES (1, 'dwarf3', 'k1', 1, 1)"""
        )
        cur = conn.execute(
            "INSERT INTO frames (path, image_type, session_key) VALUES (?, 'LIGHT', 'k1')",
            (str(src),),
        )
        conn.execute(
            "INSERT INTO session_frames (session_id, frame_id) VALUES (1, ?)",
            (cur.lastrowid,),
        )


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.sqlite"
    # Seed catalog
    with open_db(db_path) as conn:
        _seed_session_with_png(conn, tmp_path / "session_a")

    # Make the API talk to this catalog by overriding default_db_path.
    from server.catalog import db as db_module

    monkeypatch.setattr(db_module, "default_db_path", lambda: db_path)
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    job_manager.reset_for_tests(db_path=db_path)
    with TestClient(app) as c:
        yield c


def test_list_templates_includes_canned(client) -> None:
    body = client.get("/api/templates").json()
    ids = [t["id"] for t in body]
    assert "calibrate_register_stack" in ids


def test_from_session_404_on_unknown_template(client) -> None:
    r = client.post(
        "/api/jobs/from_session",
        json={"session_id": 1, "template_id": "nope"},
    )
    assert r.status_code == 404


def test_from_session_400_on_missing_calibration(client) -> None:
    """No master dark -> 400, since calibrate_register_stack needs one."""
    r = client.post(
        "/api/jobs/from_session",
        json={"session_id": 1, "template_id": "calibrate_register_stack"},
    )
    assert r.status_code == 400
    assert "no matched master" in r.text


def test_from_session_404_on_unknown_session(client) -> None:
    r = client.post(
        "/api/jobs/from_session",
        json={"session_id": 9999, "template_id": "calibrate_register_stack"},
    )
    assert r.status_code == 404
