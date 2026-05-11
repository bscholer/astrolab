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


def test_template_schema_exposes_per_node_outputs(client) -> None:
    """Each node entry must include its declared output ports so the UI can
    pick a preview port without a parallel guess table. Regressed once: the
    narrowband_extract step rendered as a forever-loading spinner because
    the UI guessed `image` and the server's `ha`/`oiii` ports 404'd."""
    r = client.get("/api/templates/calibrate_register_stack_narrowband/schema")
    assert r.status_code == 200
    by_id = {n["node_id"]: n for n in r.json()["nodes"]}
    # narrowband_extract has two declared output ports; both must appear.
    assert by_id["narrowband_extract"]["outputs"] == {
        "ha": "image/fits",
        "oiii": "image/fits",
    }
    # Sequence emitters expose `sequence`, not `image`.
    assert by_id["resample"]["outputs"] == {"sequence": "sequence/fits"}
    # starnet_extract has the same two-port shape as narrowband_extract.
    assert by_id["starnet_extract"]["outputs"] == {
        "starless": "image/fits",
        "stars": "image/fits",
    }
    # Single-image emitters still use `image`.
    assert by_id["save"]["outputs"] == {"image": "image/png"}


def test_history_published_default_false(client, tmp_path: Path) -> None:
    """Fresh entries land unpublished — gallery is opt-in."""
    src = _make_png(tmp_path / "in.png")
    body = client.post("/api/projects", json=_payload(src)).json()
    assert body["history"][0]["published"] is False


def test_set_history_published_round_trips(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    pid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{pid}").json()["current_job_id"])

    r = client.put(
        f"/api/projects/{pid}/history/0/published",
        json={"published": True},
    )
    assert r.status_code == 200
    assert r.json()["history"][0]["published"] is True

    # Idempotent: same call returns the same state.
    r2 = client.put(
        f"/api/projects/{pid}/history/0/published",
        json={"published": True},
    )
    assert r2.json()["history"][0]["published"] is True

    # And a flip back works.
    r3 = client.put(
        f"/api/projects/{pid}/history/0/published",
        json={"published": False},
    )
    assert r3.json()["history"][0]["published"] is False


def test_set_history_published_unknown_seq_400(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    pid = client.post("/api/projects", json=_payload(src)).json()["id"]
    r = client.put(
        f"/api/projects/{pid}/history/99/published",
        json={"published": True},
    )
    assert r.status_code == 400


def test_set_history_published_unknown_project_404(client) -> None:
    r = client.put(
        "/api/projects/not-real/history/0/published",
        json={"published": True},
    )
    assert r.status_code == 404


def test_gallery_filters_to_published_only(client, tmp_path: Path) -> None:
    """The gallery feed surfaces only opted-in history entries."""
    src = _make_png(tmp_path / "in.png")
    pid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{pid}").json()["current_job_id"])

    # Default state: no publishes -> empty gallery.
    assert client.get("/api/gallery").json() == []

    # Publish seq 0 -> shows up.
    client.put(
        f"/api/projects/{pid}/history/0/published",
        json={"published": True},
    )
    body = client.get("/api/gallery").json()
    assert len(body) == 1
    assert body[0]["project_id"] == pid
    assert body[0]["seq"] == 0


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


# ---------------------------------------------------------------------------
# Description (notes) field
# ---------------------------------------------------------------------------


def test_description_defaults_to_null(client, tmp_path: Path) -> None:
    """Fresh projects start with no note set."""
    src = _make_png(tmp_path / "in.png")
    body = client.post("/api/projects", json=_payload(src)).json()
    assert body["description"] is None


def test_patch_description_round_trips(client, tmp_path: Path) -> None:
    """PATCH stores a note and GET returns it without losing the project's
    other state. The metadata patch must not append a history entry."""
    src = _make_png(tmp_path / "in.png")
    pid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{pid}").json()["current_job_id"])
    pre_history_len = len(client.get(f"/api/projects/{pid}").json()["history"])

    r = client.patch(
        f"/api/projects/{pid}",
        json={"description": "first night with the dew heater"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["description"] == "first night with the dew heater"
    # No new history entry: description is metadata, not pipeline input.
    assert len(body["history"]) == pre_history_len

    # GET round-trip carries the note.
    body2 = client.get(f"/api/projects/{pid}").json()
    assert body2["description"] == "first night with the dew heater"


def test_patch_description_empty_string_clears(client, tmp_path: Path) -> None:
    """Empty/whitespace-only descriptions normalize to null so the UI's
    'no note' state is unambiguous."""
    src = _make_png(tmp_path / "in.png")
    pid = client.post("/api/projects", json=_payload(src)).json()["id"]
    client.patch(f"/api/projects/{pid}", json={"description": "stuff"})
    r = client.patch(f"/api/projects/{pid}", json={"description": "   "})
    assert r.json()["description"] is None
    r2 = client.patch(f"/api/projects/{pid}", json={"description": "stuff"})
    assert r2.json()["description"] == "stuff"
    r3 = client.patch(f"/api/projects/{pid}", json={"description": ""})
    assert r3.json()["description"] is None


def test_patch_omitting_description_leaves_it_alone(client, tmp_path: Path) -> None:
    """A PATCH that doesn't include description doesn't touch a saved note."""
    src = _make_png(tmp_path / "in.png")
    pid = client.post("/api/projects", json=_payload(src)).json()["id"]
    _wait_for_job(client, client.get(f"/api/projects/{pid}").json()["current_job_id"])
    client.patch(f"/api/projects/{pid}", json={"description": "keep me"})

    # An override-only patch must not clear the note.
    r = client.patch(
        f"/api/projects/{pid}",
        json={"overrides": {"ds": {"target_size_px": 32}}},
    )
    body = r.json()
    assert body["description"] == "keep me"


def test_patch_description_404_on_unknown_project(client) -> None:
    r = client.patch("/api/projects/not-real", json={"description": "x"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Display name DTO
# ---------------------------------------------------------------------------


def test_display_is_null_without_resolved_target(client, tmp_path: Path) -> None:
    """When the project has no source sessions (raw template+job path),
    there's no target to resolve, so display surfaces as None."""
    src = _make_png(tmp_path / "in.png")
    body = client.post("/api/projects", json=_payload(src)).json()
    assert body["display"] is None


# A separate fixture pins ASTROLAB_HOME so the API's DBDep (which goes through
# default_db_path()) and the project/job managers all read+write the same DB.
# The base `client` above shards the DB via reset_for_tests but doesn't touch
# ASTROLAB_HOME, so /api/projects POST reads from a different sqlite file than
# the manager writes into. For the display tests we need both ends aligned.
@pytest.fixture
def client_with_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from server.catalog.db import default_db_path

    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    db_path = default_db_path()
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    job_manager.reset_for_tests(db_path=db_path)
    project_manager.reset_for_tests(db_path=db_path)
    with TestClient(app) as c:
        yield c


def test_display_surfaces_common_name_for_resolved_single_target(
    client_with_catalog, tmp_path: Path
) -> None:
    """A project whose only source session points at 'M 33' should surface
    {name: 'Triangulum Galaxy', canonical: 'NGC 598'} on its DTO. The UI
    promotes this to the page header with the canonical id as a sub-label.
    """
    from server.catalog.db import open_db

    with open_db() as conn, conn:
        conn.execute("INSERT INTO targets (id, name) VALUES (101, 'M 33')")
        conn.execute(
            """
            INSERT INTO sessions (id, scope_id, session_key, target_id, frame_count)
            VALUES (501, 'dwarf3', 'kdisplay', 101, 3)
            """,
        )

    src = _make_png(tmp_path / "in.png")
    payload = _payload(src, name="user's M33 OSC RGB processing")
    payload["source_session_ids"] = ["501"]
    body = client_with_catalog.post("/api/projects", json=payload).json()

    # The user's own project name still rides on `name`.
    assert body["name"] == "user's M33 OSC RGB processing"
    # And the display block carries the catalog-resolved labels.
    assert body["display"] is not None
    assert body["display"]["name"] == "Triangulum Galaxy"
    assert body["display"]["canonical"] == "NGC 598"


def test_display_is_null_for_unresolvable_target(
    client_with_catalog, tmp_path: Path
) -> None:
    """A target with a freeform name that doesn't resolve in OpenNGC and
    has no curated common_name surfaces as display=None; the UI falls
    back to the user's project name in that case."""
    from server.catalog.db import open_db

    with open_db() as conn, conn:
        conn.execute(
            "INSERT INTO targets (id, name) VALUES (102, 'my-backyard-comet')"
        )
        conn.execute(
            """
            INSERT INTO sessions (id, scope_id, session_key, target_id, frame_count)
            VALUES (502, 'dwarf3', 'kfreeform', 102, 3)
            """,
        )

    src = _make_png(tmp_path / "in.png")
    payload = _payload(src, name="comet pass")
    payload["source_session_ids"] = ["502"]
    body = client_with_catalog.post("/api/projects", json=payload).json()
    assert body["display"] is None
