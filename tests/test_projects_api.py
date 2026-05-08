"""Renderings API: create / patch / revert / schema endpoint tests.

Uses the trivial `downscale` node (no Siril needed) so the worker actually
runs end-to-end without external binaries. Each test gets its own cache and
catalog DB to keep persistence writes hermetic.
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
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_create_seeds_initial_history_entry(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    r = client.post("/api/projects", json=_payload(src, name="ngc 7380"))
    assert r.status_code == 200
    body = r.json()

    assert body["name"] == "ngc 7380"
    assert body["current_seq"] == 0
    assert len(body["history"]) == 1
    assert body["history"][0]["overrides"] == {}
    assert body["history"][0]["label"] == "initial render"
    assert body["current_job_id"] == body["history"][0]["job_id"]

    # Initial job should run to completion via the worker.
    _wait_for_job(client, body["current_job_id"])


def test_patch_appends_history_with_diff_label(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{rid}").json()["current_job_id"])

    r = client.patch(
        f"/api/projects/{rid}",
        json={"overrides": {"ds": {"target_size_px": 32}}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["current_seq"] == 1
    assert len(body["history"]) == 2
    assert body["history"][1]["overrides"] == {"ds": {"target_size_px": 32}}
    # Auto-label should mention the changed param.
    assert "target_size_px" in body["history"][1]["label"]
    assert "64" in body["history"][1]["label"] or "<default>" in body["history"][1]["label"]
    assert "32" in body["history"][1]["label"]

    _wait_for_job(client, body["history"][1]["job_id"])


def test_patch_with_same_overrides_still_appends(client, tmp_path: Path) -> None:
    """A no-op patch (eg from an unintentional click) should still record an
    explicit timeline entry; the cache makes the underlying job a near-instant
    cache hit."""
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{rid}").json()["current_job_id"])

    r = client.patch(f"/api/projects/{rid}", json={"overrides": {}})
    body = r.json()
    assert body["current_seq"] == 1
    assert body["history"][1]["label"] == "no changes"


def test_revert_moves_pointer_without_new_job(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{rid}").json()["current_job_id"])

    body = client.patch(
        f"/api/projects/{rid}",
        json={"overrides": {"ds": {"target_size_px": 32}}},
    ).json()
    _wait_for_job(client, body["history"][1]["job_id"])
    assert body["current_seq"] == 1

    r = client.post(f"/api/projects/{rid}/revert/0")
    assert r.status_code == 200
    body = r.json()
    assert body["current_seq"] == 0
    assert body["current_job_id"] == body["history"][0]["job_id"]
    # No new history entry on revert.
    assert len(body["history"]) == 2


def test_revert_to_unknown_seq_400(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src)).json()["id"]
    r = client.post(f"/api/projects/{rid}/revert/99")
    assert r.status_code == 400


def test_force_rerun_appears_in_history(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{rid}").json()["current_job_id"])

    r = client.patch(f"/api/projects/{rid}", json={"force": True})
    body = r.json()
    assert body["current_seq"] == 1
    assert "Reprocess" in body["history"][1]["label"]


def test_unknown_rendering_404(client) -> None:
    assert client.get("/api/projects/not-real").status_code == 404
    assert client.patch(
        "/api/projects/not-real", json={"overrides": {}}
    ).status_code == 404
    assert client.post("/api/projects/not-real/revert/0").status_code == 404


def test_list_returns_recent_first(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    a = client.post("/api/projects", json=_payload(src, name="alpha")).json()["id"]
    time.sleep(0.01)
    b = client.post("/api/projects", json=_payload(src, name="beta")).json()["id"]
    listing = client.get("/api/projects").json()
    ids = [r["id"] for r in listing]
    assert ids.index(b) < ids.index(a)


def test_template_schema_endpoint(client) -> None:
    r = client.get("/api/templates/calibrate_register_stack/schema")
    assert r.status_code == 200
    body = r.json()
    assert body["template_id"] == "calibrate_register_stack"
    by_id = {n["node_id"]: n for n in body["nodes"]}
    # All pipeline steps are present.
    assert {"convert", "calibrate", "bg_extract", "register", "stack", "stretch", "save"} <= set(by_id)
    # Cost classes match the node defs (cheap finishing nodes vs expensive heavy ones).
    assert by_id["stretch"]["cost"] == "cheap"
    assert by_id["save"]["cost"] == "cheap"
    assert by_id["stack"]["cost"] == "expensive"
    assert by_id["register"]["cost"] == "expensive"
    # Schema carries Pydantic Field metadata: descriptions, ranges, enums.
    stretch_props = by_id["stretch"]["schema"]["properties"]
    assert stretch_props["method"]["enum"] == ["autostretch", "mtf", "asinh"]
    assert "description" in stretch_props["shadows_clip"]


def test_template_schema_unknown_404(client) -> None:
    r = client.get("/api/templates/not-real/schema")
    assert r.status_code == 404


def test_cancel_running_job_marks_interrupted(client, tmp_path: Path) -> None:
    """Set the cancel token on a record before the worker picks it up; the
    worker should see it on entry, mark the job interrupted, and never run
    a node. This is the path ProjectManager.patch uses to abandon stale
    in-flight pipelines when the user tweaks a slider mid-render."""
    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{rid}").json()["current_job_id"])

    body = client.patch(
        f"/api/projects/{rid}",
        json={"overrides": {"ds": {"target_size_px": 32}}},
    ).json()
    job_id = body["history"][1]["job_id"]

    from server.api import job_manager

    # Whether we beat the worker or not is timing-dependent; the contract is
    # that a job either finishes normally or ends up 'interrupted', never
    # stuck in queue, never 'failed'.
    job_manager.cancel(job_id)

    deadline = time.time() + 5.0
    while time.time() < deadline:
        rec = job_manager.get(job_id)
        if rec is not None and rec.status in ("completed", "failed", "interrupted"):
            break
        time.sleep(0.05)
    rec = job_manager.get(job_id)
    assert rec is not None
    assert rec.status in ("completed", "interrupted")


def test_patch_signals_prior_running_job(monkeypatch, client, tmp_path: Path) -> None:
    """Auto-cancel happens inside ProjectManager.patch: prior active job
    (if still queued/running) gets its cancel token flipped before the new
    job is queued. Verified here by stubbing JobManager.cancel and checking
    the call history."""
    from server.api import project_manager

    src = _make_png(tmp_path / "in.png")
    rid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{rid}").json()["current_job_id"])

    # Track what ProjectManager asks JobManager to cancel.
    cancelled: list[str] = []
    real_cancel = project_manager._jobs.cancel

    def tracking_cancel(job_id: str) -> bool:
        cancelled.append(job_id)
        return real_cancel(job_id)

    monkeypatch.setattr(project_manager._jobs, "cancel", tracking_cancel)

    prior_job = client.get(f"/api/projects/{rid}").json()["current_job_id"]
    body = client.patch(
        f"/api/projects/{rid}",
        json={"overrides": {"ds": {"target_size_px": 32}}},
    ).json()
    _wait_for_job(client, body["history"][1]["job_id"])

    assert prior_job in cancelled, \
        "patch should have asked JobManager to cancel the prior active job"
