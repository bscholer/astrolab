"""Storage API tests: size accounting, per-rendering purge, eviction.

Uses the trivial `downscale` node (no Siril) so jobs run end-to-end in
the test process. Each test gets its own cache + catalog DB to keep the
storage accounting hermetic.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import nodes.basic  # noqa: F401  registers downscale
from server.api import app, job_manager, project_manager
from server.cache import ContentCache


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    job_manager.reset_for_tests(db_path=tmp_path / "catalog.sqlite")
    project_manager.reset_for_tests(db_path=tmp_path / "catalog.sqlite")
    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _make_png(path: Path, size: tuple[int, int] = (200, 100)) -> Path:
    Image.new("RGB", size, (32, 64, 96)).save(path)
    return path


def _payload(src_png: Path, target_size: int = 64, name: str = "test") -> dict:
    return {
        "name": name,
        "template": {
            "id": "test_downscale",
            "version": 1,
            "description": "smoke",
            "nodes": [
                {"id": "ds", "kind": "downscale", "params": {"target_size_px": target_size}},
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
        "source_session_ids": [],
    }


def _wait_for_job(client, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "interrupted"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def _wait_for_current(client, rid: str) -> None:
    body = client.get(f"/api/projects/{rid}").json()
    _wait_for_job(client, body["current_job_id"])


def test_storage_snapshot_lists_per_project_owned_bytes(client, tmp_path: Path) -> None:
    """A fresh rendering's owned_bytes should be > 0 after its job runs and
    its node hash is captured + cache committed."""
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src, name="alpha")).json()["id"]
    _wait_for_current(client, rid)

    snap = client.get("/api/storage").json()
    assert snap["total_bytes"] > 0
    by_id = {p["project_id"]: p for p in snap["per_project"]}
    assert rid in by_id
    assert by_id[rid]["owned_bytes"] > 0
    assert by_id[rid]["entry_count"] >= 1


def test_two_projects_share_when_inputs_match(client, tmp_path: Path) -> None:
    """Two projects pointing at the same source PNG with identical params
    should produce overlapping cache entries — one project's owned bytes
    drop and shared bytes grow."""
    src = _make_png(tmp_path / "in.png")
    a_id = client.post("/api/projects", json=_payload(src, name="alpha")).json()["id"]
    _wait_for_current(client, a_id)
    b_id = client.post("/api/projects", json=_payload(src, name="beta")).json()["id"]
    _wait_for_current(client, b_id)

    snap = client.get("/api/storage").json()
    by_id = {p["project_id"]: p for p in snap["per_project"]}
    assert by_id[a_id]["shared_bytes"] > 0
    assert by_id[b_id]["shared_bytes"] > 0
    # Owned drops to zero when content is fully shared between two
    # identical-input renderings.
    assert by_id[a_id]["owned_bytes"] == 0


def test_delete_rendering_frees_owned_cache(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src, name="alpha")).json()["id"]
    _wait_for_current(client, rid)

    pre = client.get("/api/storage").json()["total_bytes"]
    r = client.delete(f"/api/projects/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["evicted_count"] >= 1
    assert body["bytes_freed"] > 0
    post = client.get("/api/storage").json()["total_bytes"]
    assert post < pre

    # Rendering itself is gone too.
    assert client.get(f"/api/projects/{rid}").status_code == 404


def test_delete_rendering_keeps_shared_cache(client, tmp_path: Path) -> None:
    """Deleting one project should not evict cache entries that another
    project still references."""
    src = _make_png(tmp_path / "in.png")
    a_id = client.post("/api/projects", json=_payload(src, name="alpha")).json()["id"]
    _wait_for_current(client, a_id)
    b_id = client.post("/api/projects", json=_payload(src, name="beta")).json()["id"]
    _wait_for_current(client, b_id)

    pre = client.get("/api/storage").json()
    pre_total = pre["total_bytes"]
    pre_count = pre["entry_count"]

    client.delete(f"/api/projects/{a_id}")
    post = client.get("/api/storage").json()
    # Total bytes unchanged: every entry was shared with beta.
    assert post["total_bytes"] == pre_total
    assert post["entry_count"] == pre_count
    # Beta's owned_bytes climbs since the entries are now solely hers.
    by_id = {p["project_id"]: p for p in post["per_project"]}
    assert by_id[b_id]["owned_bytes"] > 0


def test_purge_intermediates_keeps_terminal_outputs(client, tmp_path: Path) -> None:
    """keep_outputs=true preserves the rendering's terminal-output cache
    hashes so the saved image survives even after intermediates evict.
    With a single-node test pipeline the terminal IS the only entry, so
    we expect zero evictions in this case — the smoke check is that the
    endpoint returns success and the rendering survives."""
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src, name="alpha")).json()["id"]
    _wait_for_current(client, rid)

    r = client.delete(f"/api/projects/{rid}/cache?keep_outputs=true")
    assert r.status_code == 200
    body = r.json()
    assert body["evicted_count"] == 0
    # Rendering still exists and its current job is intact.
    assert client.get(f"/api/projects/{rid}").status_code == 200


def test_settings_round_trip(client) -> None:
    initial = client.get("/api/settings").json()
    assert "cache_max_bytes" in initial
    assert initial["cache_max_bytes"] >= 1024 * 1024 * 1024

    new_value = 50 * 1024 * 1024 * 1024  # 50 GiB
    r = client.patch(
        "/api/settings", json={"cache_max_bytes": new_value}
    )
    assert r.status_code == 200
    assert r.json()["cache_max_bytes"] == new_value
    # Persisted: a fresh GET reads it back.
    assert client.get("/api/settings").json()["cache_max_bytes"] == new_value


def test_settings_floor_enforced(client) -> None:
    r = client.patch(
        "/api/settings", json={"cache_max_bytes": 100}
    )
    assert r.status_code == 400


def test_cleanup_under_budget_is_noop(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src, name="alpha")).json()["id"]
    _wait_for_current(client, rid)

    # Budget far above current usage.
    r = client.post(
        "/api/storage/cleanup",
        json={"max_bytes": 100 * 1024 * 1024 * 1024},
    )
    body = r.json()
    assert body["evicted_count"] == 0
    assert body["over_budget"] is False


def test_cleanup_evicts_when_over_budget(client, tmp_path: Path) -> None:
    """Force a cleanup with a tiny budget; the eviction sweep should drop
    entries until we're at or under the cap (or flag over_budget if
    nothing remains we're allowed to evict)."""
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src, name="alpha")).json()["id"]
    _wait_for_current(client, rid)

    pre = client.get("/api/storage").json()["total_bytes"]
    assert pre > 0

    r = client.post("/api/storage/cleanup", json={"max_bytes": 1})
    body = r.json()
    assert body["evicted_count"] >= 1
    assert body["bytes_freed"] > 0
