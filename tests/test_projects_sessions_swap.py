"""End-to-end coverage for the session-swap surface.

Covers:
- PATCH /api/projects/{id}/sessions  (swap, gates, eviction, history kind)
- GET   /api/projects/{id}/suggestions  (orphan sessions on same canonical)
- GET   /api/projects/{id}/cache?dry_run  (eviction-bytes preview)
- The `suggested_additions` block inline on the project DTO

Test approach mirrors test_projects_from_sessions_api.py: spin a per-test
catalog DB, seed sessions with PNG fixtures, create a project via the
real API, then exercise the swap path. We use the trivial downscale
template (no Siril) for the happy paths so the worker actually runs end
to end; calibration is mode='none' to bypass the master matcher entirely.
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
    resolved_canonical: str | None = "NGC 598",
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i in range(n_frames):
        p = folder / f"frame_{i}.png"
        Image.new("RGB", (200, 100), (32, 64, 96)).save(p)
        paths.append(p)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO targets (id, name, resolved_canonical) VALUES (?, ?, ?)",
            (target_id, target_name, resolved_canonical),
        )
        conn.execute(
            """
            INSERT INTO sessions
            (id, scope_id, target_id, instrument, exptime, gain,
             binning, filter, frame_count, failed_count)
            VALUES (?, 'dwarf3', ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                session_id,
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
                "INSERT INTO frames (path, image_type) VALUES (?, 'LIGHT')",
                (str(p),),
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


def _make_png(path: Path) -> Path:
    Image.new("RGB", (200, 100), (32, 64, 96)).save(path)
    return path


def _drain_job(c, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = c.get(f"/api/jobs/{job_id}").json()
        if body.get("status") in ("completed", "failed", "interrupted"):
            time.sleep(0.2)  # let trailing event flush
            return
        time.sleep(0.05)
    job_manager.cancel(job_id)
    time.sleep(0.3)


def _wait_for_project_job(c, project_id: str, timeout: float = 5.0) -> None:
    body = c.get(f"/api/projects/{project_id}").json()
    _drain_job(c, body["current_job_id"], timeout=timeout)


# ---------------------------------------------------------------------------
# Suggestions endpoint + inline payload
# ---------------------------------------------------------------------------


def test_suggestions_inline_on_project_response(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
        _seed_session(conn, session_id=3, folder=tmp_path / "s3")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    body = c.get(f"/api/projects/{pid}").json()
    sugg = body["suggested_additions"]
    assert sugg is not None
    assert sorted(sugg["session_ids"]) == [2, 3]
    assert sugg["session_count"] == 2
    assert sugg["frame_count"] == 6  # 3 + 3
    assert sugg["integration_seconds"] == pytest.approx(180.0)  # 2 * 3 * 30
    assert sugg["suggestions_token"]


def test_suggestions_endpoint_returns_orphans(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    sugg = c.get(f"/api/projects/{pid}/suggestions").json()
    assert sugg["session_ids"] == [2]
    assert sugg["session_count"] == 1
    assert sugg["frame_count"] == 3
    assert sugg["integration_seconds"] == pytest.approx(90.0)
    assert sugg["suggestions_token"]


def test_suggestions_skips_compat_incompatible_orphans(client) -> None:
    """A same-target orphan whose gain/filter/binning would fail the
    PATCH endpoint's compat gate must NOT show up in the suggestion
    list. The user shouldn't get a banner that 400s on accept.

    `exptime` is deliberately NOT in the compat tuple anymore: the
    calibrate node picks the right dark per (exptime, temp) bin, so
    mixed-exposure bundles are valid. This test pins gain instead.
    """
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1", gain=60)
        # Same target + canonical, but gain differs -> would 400 on PATCH.
        _seed_session(conn, session_id=2, folder=tmp_path / "s2", gain=80)
        # Same target + canonical AND gain matches -> should appear.
        _seed_session(conn, session_id=3, folder=tmp_path / "s3", gain=60)
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    sugg = c.get(f"/api/projects/{pid}/suggestions").json()
    assert sugg["session_ids"] == [3], (
        "expected compat-matched orphan only; got: "
        + repr(sugg["session_ids"])
    )


def test_suggestions_empty_when_no_orphans(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    sugg = c.get(f"/api/projects/{pid}/suggestions").json()
    assert sugg["session_ids"] == []
    assert sugg["session_count"] == 0
    body = c.get(f"/api/projects/{pid}").json()
    assert body["suggested_additions"] is None


def test_suggestions_empty_for_multi_target_project(client) -> None:
    """Multi-target projects can't suggest additions because there's no
    single canonical_group to query against."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(
            conn, session_id=1, folder=tmp_path / "s1",
            target_id=1, target_name="M 33", resolved_canonical="NGC 598",
        )
        _seed_session(
            conn, session_id=2, folder=tmp_path / "s2",
            target_id=2, target_name="M 31", resolved_canonical="NGC 224",
        )
        # Force the from_sessions compat checker to accept this by sharing
        # all other fields. (target_id matters for compat too: same target
        # required for the multi-session compat gate.)
    # We can't easily make /from_sessions accept two different targets;
    # they'd fail the COMPAT_FIELDS gate. So we use a single-session
    # project to set up state, then poke directly through the manager.
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])
    # Mutate source_session_ids on the in-memory record to simulate a
    # multi-target project. We have to bypass the API here because the
    # API enforces single-target everywhere.
    project = project_manager.get(pid)
    project.source_session_ids = ["1", "2"]

    sugg = c.get(f"/api/projects/{pid}/suggestions").json()
    assert sugg["session_ids"] == []


def test_suggestions_token_is_deterministic(client) -> None:
    """Same id list -> same token; new session captured -> token changes."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    t1 = c.get(f"/api/projects/{pid}/suggestions").json()["suggestions_token"]
    t2 = c.get(f"/api/projects/{pid}/suggestions").json()["suggestions_token"]
    assert t1 == t2 and t1

    # A new orphan session lands -> token shifts so a dismissed banner
    # comes back into view.
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=3, folder=tmp_path / "s3")
    t3 = c.get(f"/api/projects/{pid}/suggestions").json()["suggestions_token"]
    assert t3 and t3 != t1


# ---------------------------------------------------------------------------
# PATCH /sessions: gates
# ---------------------------------------------------------------------------


def test_patch_sessions_400_on_empty_list(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [], "auto_render": False},
    )
    assert swap.status_code == 400
    assert "empty" in swap.json()["detail"]


def test_patch_sessions_404_on_unknown_project(client) -> None:
    c, *_ = client
    r = c.patch(
        "/api/projects/does-not-exist/sessions",
        json={"session_ids": [1], "auto_render": False},
    )
    assert r.status_code == 404


def test_from_sessions_rejects_cross_canonical_bundle(client) -> None:
    """POST /from_sessions enforces canonical-group equality across the
    bundle. Two sessions on truly different canonicals are rejected with
    a 400 naming the offending session + both canonicals."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(
            conn, session_id=1, folder=tmp_path / "s1",
            target_id=1, target_name="M 33", resolved_canonical="NGC 598",
        )
        _seed_session(
            conn, session_id=2, folder=tmp_path / "s2",
            target_id=2, target_name="M 31", resolved_canonical="NGC 224",
        )
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1, 2],
            "template_id": "calibrate_register_stack",
        },
    )
    assert r.status_code == 400, r.json()
    detail = r.json()["detail"]
    assert "NGC 598" in detail and "NGC 224" in detail


def test_from_sessions_accepts_retargeted_sessions_with_same_canonical(client) -> None:
    """Regression: when a user re-targets a session via the Library edit
    affordance, the raw target_id row differs from the original session's
    target_id, but both resolve to the same canonical group. The old
    target_id equality check in build_from_sessions rejected this; the
    new canonical-group check at the API layer accepts it correctly."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        # Two distinct target rows that BOTH resolve to canonical "NGC 598",
        # mimicking the state after a manual re-target merges them onto the
        # same astronomical object.
        _seed_session(
            conn, session_id=1, folder=tmp_path / "s1",
            target_id=1, target_name="M 33", resolved_canonical="NGC 598",
        )
        _seed_session(
            conn, session_id=2, folder=tmp_path / "s2",
            target_id=2, target_name="Triangulum Galaxy",
            resolved_canonical="NGC 598",
        )
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1, 2],
            "template_id": "calibrate_register_stack",
        },
    )
    assert r.status_code == 200, r.json()


def test_patch_sessions_400_on_cross_target(client) -> None:
    """A session whose canonical_group differs from the project's is
    rejected with a descriptive message naming the offending session,
    its target, and the two canonicals."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(
            conn, session_id=1, folder=tmp_path / "s1",
            target_id=1, target_name="M 33", resolved_canonical="NGC 598",
        )
        # A session on a different canonical bucket.
        _seed_session(
            conn, session_id=2, folder=tmp_path / "s2",
            target_id=2, target_name="M 31", resolved_canonical="NGC 224",
        )
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": False},
    )
    assert swap.status_code == 400
    detail = swap.json()["detail"]
    assert "session 2" in detail
    assert "NGC 224" in detail
    assert "NGC 598" in detail


def test_patch_sessions_400_on_incompatible_gain(client) -> None:
    """The build_from_sessions calibration-compat gate still bites even
    when both sessions share canonical_group."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1", gain=60)
        _seed_session(conn, session_id=2, folder=tmp_path / "s2", gain=80)
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": False},
    )
    assert swap.status_code == 400
    assert "gain" in swap.json()["detail"]


# ---------------------------------------------------------------------------
# PATCH /sessions: happy paths
# ---------------------------------------------------------------------------


def test_patch_sessions_adds_session_and_appends_history(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": False},
    )
    assert swap.status_code == 200, swap.text
    body = swap.json()
    assert sorted(body["source_session_ids"]) == ["1", "2"]
    # History grew with a swap_sessions entry.
    assert len(body["history"]) >= 2
    last = body["history"][-1]
    assert last["kind"] == "swap_sessions"
    assert sorted(last["snapshot"]["session_ids"]) == [1, 2]
    assert last["snapshot"]["frame_count"] == 6
    assert last["snapshot"]["integration_seconds"] == pytest.approx(180.0)
    # Frame count + integration time on the capture roll-up reflect the new bundle.
    assert body["capture"]["frame_count"] == 6
    assert body["capture"]["integration_seconds"] == pytest.approx(180.0)


def test_patch_sessions_lifts_framework_overrides_into_revision(client, tmp_path) -> None:
    """Regression: rebuilding the base_job via PATCH /sessions attaches
    framework-level overrides (calibrate.dark_bins from match_bundle_darks).
    Those used to be silently dropped because swap_sessions stored the
    new base_job without merging its param_overrides into the running
    revision's overrides, and _submit_with_overrides replaces rather
    than merges. After the lift, the rebuilt overrides are visible to
    the next render and the stored base_job has an empty
    param_overrides (matching the create() contract).

    We seed a master that match_bundle_darks will pick up so the
    rebuild actually has framework metadata to lift; without a
    matched master, dark_bins would be empty either way and the test
    would be a no-op.
    """
    c, db_path, tmp_path = client
    masters_dir = tmp_path / "masters"
    masters_dir.mkdir()
    dark_path = masters_dir / "dark.fit"
    dark_path.write_bytes(b"MASTER")
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
        conn.execute(
            "INSERT INTO masters (kind, source, instrument, exptime, gain, "
            "binning, ccd_temp, stack_count, path) "
            "VALUES ('dark','factory','DWARFIII',30.0,60,1,28.0,10,?)",
            (str(dark_path),),
        )
        conn.commit()

    r = c.post(
        "/api/projects/from_sessions",
        json={"session_ids": [1], "template_id": "calibrate_register_stack"},
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": False},
    )
    assert swap.status_code == 200, swap.text
    body = swap.json()

    # Stored base_job carries no param_overrides (lift-and-clear).
    assert body["base_job"]["param_overrides"] == {}
    # Revision's overrides now carry the framework metadata.
    last = body["history"][-1]
    cal = last["overrides"].get("calibrate") or {}
    assert "dark_bins" in cal, (
        f"expected calibrate.dark_bins in revision overrides, "
        f"got: {last['overrides']!r}"
    )
    assert any(
        b.get("path") == str(dark_path) for b in cal["dark_bins"]
    ), f"expected the matched dark in dark_bins, got: {cal['dark_bins']!r}"


def test_patch_sessions_remove_all_but_one_works(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1, 2],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1], "auto_render": False},
    )
    assert swap.status_code == 200, swap.text
    assert swap.json()["source_session_ids"] == ["1"]


def test_patch_sessions_auto_render_false_does_not_kick_a_new_job(client) -> None:
    """auto_render=False: history advances but no new job is submitted.
    The new history entry carries the prior job_id so the UI stays
    attached to the last rendered output."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    pre = c.get(f"/api/projects/{pid}").json()
    pre_job = pre["current_job_id"]

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": False},
    )
    assert swap.status_code == 200, swap.text
    body = swap.json()
    assert body["new_job_id"] is None
    # The new history entry reuses the prior job_id so the UI stays
    # attached to the last rendered output until the user kicks a render.
    assert body["history"][-1]["job_id"] == pre_job


def test_patch_sessions_auto_render_true_submits_job(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    pre_job = c.get(f"/api/projects/{pid}").json()["current_job_id"]
    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": True},
    )
    assert swap.status_code == 200, swap.text
    body = swap.json()
    assert body["new_job_id"] is not None
    assert body["new_job_id"] != pre_job
    _drain_job(c, body["new_job_id"])


# ---------------------------------------------------------------------------
# PATCH /sessions: cache eviction
# ---------------------------------------------------------------------------


def test_patch_sessions_returns_eviction_stats(client) -> None:
    """The swap response carries evicted_count + evicted_bytes so the UI
    can confirm what the cache nuke freed."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": False},
    )
    body = swap.json()
    assert "evicted_count" in body
    assert "evicted_bytes" in body
    assert isinstance(body["evicted_count"], int)
    assert isinstance(body["evicted_bytes"], int)


# ---------------------------------------------------------------------------
# GET /cache: dry-run preview
# ---------------------------------------------------------------------------


def test_cache_dry_run_returns_eviction_preview(client) -> None:
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    body = c.get(f"/api/projects/{pid}/cache").json()
    assert "evicted_count" in body
    assert "bytes_to_free" in body
    assert isinstance(body["evicted_count"], int)


def test_cache_dry_run_404_on_unknown_project(client) -> None:
    c, *_ = client
    r = c.get("/api/projects/does-not-exist/cache")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Revert + new history kind
# ---------------------------------------------------------------------------


def test_swap_rolls_back_when_build_fails_midflight(
    monkeypatch, client
) -> None:
    """If build_from_sessions raises after we've validated gates but
    before the manager swings the project pointer, the project's
    source_session_ids and base_job must stay on the prior state. We
    monkeypatch the builder to raise after the validations have run,
    and confirm no state changed."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    pre = c.get(f"/api/projects/{pid}").json()
    pre_sessions = pre["source_session_ids"]
    pre_history_len = len(pre["history"])
    pre_job = pre["current_job_id"]

    from server import api as api_module
    from server.job_builder import JobBuildError

    def _boom(*args, **kwargs):
        raise JobBuildError("simulated build failure")

    monkeypatch.setattr(api_module, "build_from_sessions", _boom)

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": False},
    )
    assert swap.status_code == 400
    assert "simulated" in swap.json()["detail"]

    # State unchanged.
    post = c.get(f"/api/projects/{pid}").json()
    assert post["source_session_ids"] == pre_sessions
    assert len(post["history"]) == pre_history_len
    assert post["current_job_id"] == pre_job


def test_revert_works_with_swap_history_entry(client) -> None:
    """Reverting onto a swap_sessions entry must not crash. The pointer
    moves; source_session_ids stay on the current swap (intentional;
    history snapshots are stored for a future full revert)."""
    c, db_path, tmp_path = client
    with open_db(db_path) as conn:
        _seed_session(conn, session_id=1, folder=tmp_path / "s1")
        _seed_session(conn, session_id=2, folder=tmp_path / "s2")
    r = c.post(
        "/api/projects/from_sessions",
        json={
            "session_ids": [1],
            "template_id": "calibrate_register_stack",
        },
    )
    pid = r.json()["id"]
    _drain_job(c, r.json()["current_job_id"])

    swap = c.patch(
        f"/api/projects/{pid}/sessions",
        json={"session_ids": [1, 2], "auto_render": False},
    )
    assert swap.status_code == 200
    swap_seq = swap.json()["history"][-1]["seq"]

    # Revert to the initial entry.
    rev = c.post(f"/api/projects/{pid}/revert/0")
    assert rev.status_code == 200
    assert rev.json()["current_seq"] == 0

    # And forward back to the swap entry.
    rev2 = c.post(f"/api/projects/{pid}/revert/{swap_seq}")
    assert rev2.status_code == 200
    assert rev2.json()["current_seq"] == swap_seq
