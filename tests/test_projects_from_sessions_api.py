"""End-to-end coverage for POST /api/projects/from_sessions.

We can't run the real calibrate_register_stack template (no Siril on the test
box), so the assertions live at the endpoint surface: 400 for incompatible
bundles, 200 with the canonical session_ids order for compatible ones.

Note: when the API returns 200 we drain the project's current job before the
test ends — the worker would otherwise still be writing failure events into
the next test's DB (its mock-PNG seed isn't FITS, so convert_lights bails
async). The drain keeps the per-test fixture isolation honest on slower CI
runners; locally the race tends to resolve in our favor, on GH Actions it
doesn't.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import nodes.basic  # noqa: F401
from server.api import app, job_manager, project_manager
from server.cache import ContentCache
from server.catalog.db import open_db


def _seed_session(
    conn,
    *,
    session_id: int,
    folder: Path,
    target_id: int = 1,
    target_name: str = "M 33",
    instrument: str = "DWARFIII",
    exptime: float = 30.0,
    gain: int = 60,
    binning: int = 1,
    filter_name: str | None = None,
    n_frames: int = 3,
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i in range(n_frames):
        p = folder / f"frame_{i}.png"
        Image.new("RGB", (200, 100), (32, 64, 96)).save(p)
        paths.append(p)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO targets (id, name) VALUES (?, ?)",
            (target_id, target_name),
        )
        conn.execute(
            """
            INSERT INTO sessions
            (id, scope_id, session_key, target_id, instrument, exptime, gain,
             binning, filter, frame_count, failed_count)
            VALUES (?, 'dwarf3', ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                session_id,
                f"k{session_id}",
                target_id,
                instrument,
                exptime,
                gain,
                binning,
                filter_name,
                n_frames,
            ),
        )
        for p in paths:
            cur = conn.execute(
                "INSERT INTO frames (path, image_type, session_key) "
                "VALUES (?, 'LIGHT', ?)",
                (str(p), f"k{session_id}"),
            )
            conn.execute(
                "INSERT INTO session_frames (session_id, frame_id) VALUES (?, ?)",
                (session_id, cur.lastrowid),
            )


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.sqlite"

    from server.catalog import db as db_module

    monkeypatch.setattr(db_module, "default_db_path", lambda: db_path)
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    job_manager.reset_for_tests(db_path=db_path)
    project_manager.reset_for_tests(db_path=db_path)
    with TestClient(app) as c:
        yield c, db_path, tmp_path


def _drain_project_job(client, project_body, timeout: float = 5.0) -> None:
    """Wait for the project's current job to reach a terminal status AND
    flush its event-persist tail.

    The from_sessions endpoint kicks off a real `calibrate_register_stack`
    pipeline — convert_lights bails on our PNG-fixture seed and the job
    lands in 'failed', but if we don't wait, that async write happens
    after fixture teardown and pollutes the next test's DB. The status
    field flips to 'failed' a moment before _persist_event runs (the
    worker writes the in-memory record before the trailing event), so
    after we see terminal status we still need a small grace window for
    the SQLite write to land.
    """
    job_id = project_body.get("current_job_id")
    if not job_id:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body.get("status") in ("completed", "failed", "interrupted"):
            # 200ms covers the in-memory -> SQLite gap on the slow CI runner;
            # locally it's usually <10ms but it's cheap insurance.
            time.sleep(0.2)
            return
        time.sleep(0.05)
    # Worker is wedged. Cancel + grace so any terminal event still flushes
    # before the test exits.
    job_manager.cancel(job_id)
    time.sleep(0.3)


def test_from_sessions_400_on_empty(client) -> None:
    c, *_ = client
    r = c.post(
        "/api/projects/from_sessions",
        json={"session_ids": [], "template_id": "calibrate_register_stack"},
    )
    assert r.status_code == 400


def test_from_sessions_404_on_unknown_session(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1, 999],
            "template_id": "calibrate_register_stack",
        },
    )
    assert r.status_code == 404


def test_from_sessions_400_on_incompatible_gain(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1", gain=60)
        _seed_session(conn, session_id=2, folder=tmp_path / "s2", gain=80)
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1, 2],
            "template_id": "calibrate_register_stack",
        },
    )
    assert r.status_code == 400
    assert "gain" in r.json()["detail"]


def test_from_sessions_succeeds_for_compatible_bundle(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [2, 1],
            "template_id": "calibrate_register_stack",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Stored in canonical (sorted, de-duped) order so re-runs hit the same
    # cache lineage regardless of UI selection order.
    assert body["source_session_ids"] == ["1", "2"]
    # Default name surfaces both the target and the bundle size so it's
    # distinguishable from a single-session project of the same target.
    assert body["name"] == "M 33 (2 sessions)"
    _drain_project_job(c, body)


def test_from_sessions_dedup_then_single_session_naming(client) -> None:
    """Bundle of dupes collapses to a single session — no '(1 sessions)' name."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1, 1],
            "template_id": "calibrate_register_stack",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_session_ids"] == ["1"]
    assert body["name"] == "M 33"
    _drain_project_job(c, body)
