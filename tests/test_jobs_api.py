"""Jobs API + WebSocket tests.

We submit a tiny no-Siril pipeline (downscale) so the worker actually runs
end-to-end without needing a real Siril binary. The catalog DB is stubbed
empty so the API can boot without filesystem state.
"""

from __future__ import annotations

import time
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import nodes.basic  # noqa: F401  registers downscale
from server.api import app, job_manager
from server.cache import ContentCache


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Each test gets a clean cache and a fresh executor; the lifespan handler
    # shuts the previous one down between TestClient sessions.
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    monkeypatch.setattr(job_manager, "_records", {})
    monkeypatch.setattr(
        job_manager, "_executor",
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="astrolab-job-test"),
    )
    with TestClient(app) as c:
        yield c


def _make_png(path: Path, size: tuple[int, int] = (200, 100)) -> Path:
    Image.new("RGB", size, (32, 64, 96)).save(path)
    return path


def _downscale_payload(src_png: Path) -> dict:
    return {
        "template": {
            "id": "test_downscale",
            "version": 1,
            "description": "smoke",
            "nodes": [
                {"id": "ds", "kind": "downscale", "params": {"target_size_px": 64}},
            ],
            "outputs": {"thumb": "ds.image"},
        },
        "job": {
            "template_id": "test_downscale",
            "template_version": 1,
            "inputs": {
                "ds.image": {
                    "node_hash": "ext",
                    "port": "image",
                    "path": str(src_png),
                    "type": "image/png",
                }
            },
        },
    }


def _wait_for(client, job_id: str, terminal=("completed", "failed"), timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        body = r.json()
        if body["status"] in terminal:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_submit_run_complete(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    r = client.post("/api/jobs", json=_downscale_payload(src))
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    body = _wait_for(client, job_id)
    assert body["status"] == "completed"
    assert body["error"] is None
    assert body["outputs"] is not None
    thumb = body["outputs"]["thumb"]
    assert thumb["type"] == "image/png"
    assert Path(thumb["path"]).exists()


def test_event_history_replay(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    job_id = client.post("/api/jobs", json=_downscale_payload(src)).json()["job_id"]
    _wait_for(client, job_id)

    events = client.get(f"/api/jobs/{job_id}/events").json()
    types = [e["type"] for e in events]
    # Must always begin with queued + started, end with completed.
    assert types[0] == "job_queued"
    assert "job_started" in types
    assert "node_started" in types
    assert "node_completed" in types
    assert types[-1] == "job_completed"
    # Each event has a timestamp.
    assert all("timestamp" in e for e in events)


def test_get_unknown_job_404(client) -> None:
    r = client.get("/api/jobs/not-a-real-id")
    assert r.status_code == 404


def test_failing_job_marks_status(client, tmp_path: Path) -> None:
    # Point the downscale node at a path that doesn't exist; node should error.
    src = tmp_path / "missing.png"  # never created
    r = client.post("/api/jobs", json=_downscale_payload(src))
    job_id = r.json()["job_id"]
    body = _wait_for(client, job_id)
    assert body["status"] == "failed"
    assert body["error"]
    types = [e["type"] for e in client.get(f"/api/jobs/{job_id}/events").json()]
    assert "node_failed" in types
    assert types[-1] == "job_failed"


def test_websocket_streams_completion(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    job_id = client.post("/api/jobs", json=_downscale_payload(src)).json()["job_id"]
    # Wait for the worker to finish so all events are buffered for replay.
    _wait_for(client, job_id)

    seen: list[str] = []
    with client.websocket_connect(f"/api/jobs/{job_id}/events") as ws:
        for _ in range(50):
            try:
                ev = ws.receive_json()
            except Exception:
                break
            seen.append(ev["type"])
            if ev["type"] in ("job_completed", "job_failed"):
                break
    assert "job_completed" in seen
    assert "job_queued" in seen
    assert "node_completed" in seen


def test_list_jobs_returns_recent_first(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    ids = []
    for _ in range(2):
        ids.append(client.post("/api/jobs", json=_downscale_payload(src)).json()["job_id"])
        time.sleep(0.01)  # ensures distinct submitted_at timestamps
    for jid in ids:
        _wait_for(client, jid)
    listed = client.get("/api/jobs").json()
    listed_ids = [j["id"] for j in listed]
    # newest-first: the last submitted should appear first.
    assert listed_ids[0] == ids[-1]
