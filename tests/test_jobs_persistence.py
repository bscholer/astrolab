"""Persistence: jobs and events round-trip through SQLite, survive restarts."""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

import nodes.basic  # noqa: F401  registers downscale
from server.cache import ContentCache
from server.jobs import JobManager, JobWorker
from server.models import Job, NodeSpec, Ref, Template
from server.ports import PortType
from tests._jobs_helpers import background_worker


def _png(path: Path) -> Path:
    Image.new("RGB", (200, 100), (32, 64, 96)).save(path)
    return path


def _wait(mgr: JobManager, jid: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = mgr.get(jid)
        if rec and rec.status in ("completed", "failed"):
            return
        time.sleep(0.02)
    raise AssertionError(f"job {jid} did not finish")


def _downscale(src_png: Path) -> tuple[Template, Job]:
    template = Template(
        id="persist_test",
        version=1,
        description="persistence smoke",
        nodes=[NodeSpec(id="ds", kind="downscale", params={"target_size_px": 64})],
        outputs={"thumb": "ds.image"},
    )
    job = Job(
        template_id="persist_test",
        template_version=1,
        inputs={
            "ds.image": Ref(node_hash="ext", port="image", path=src_png,
                            type=PortType.IMAGE_PNG),
        },
    )
    return template, job


def test_job_and_events_persist(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")
    mgr = JobManager(cache=cache, db_path=db)
    template, job = _downscale(_png(tmp_path / "in.png"))

    with background_worker(cache, db):
        jid = mgr.submit(template, job)
        _wait(mgr, jid)

    # Reopen the DB directly and assert rows exist.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    job_rows = conn.execute("SELECT id, status FROM jobs").fetchall()
    assert len(job_rows) == 1
    assert job_rows[0]["status"] == "completed"

    event_rows = conn.execute(
        "SELECT type, seq FROM job_events WHERE job_id = ? ORDER BY seq",
        (jid,),
    ).fetchall()
    types = [r["type"] for r in event_rows]
    assert types[0] == "job_queued"
    assert types[-1] == "job_completed"
    # Sequence numbers should be dense and ordered.
    seqs = [r["seq"] for r in event_rows]
    assert seqs == list(range(len(seqs)))


def test_rehydrate_loads_completed_job(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")

    mgr1 = JobManager(cache=cache, db_path=db)
    template, job = _downscale(_png(tmp_path / "in.png"))
    with background_worker(cache, db):
        jid = mgr1.submit(template, job)
        _wait(mgr1, jid)

    # Open a fresh manager against the same DB and verify the job (and
    # its events) survive. The new JobRecord doesn't preload events, so
    # we pull them via get_events().
    mgr2 = JobManager(cache=cache, db_path=db)
    rec = mgr2.get(jid)
    assert rec is not None
    assert rec.status == "completed"
    assert rec.outputs is not None
    assert "thumb" in rec.outputs

    events = mgr2.get_events(jid)
    types = [e.type for e in events]
    assert types[0] == "job_queued"
    assert types[-1] == "job_completed"


def test_worker_reclaims_stale_running_job(tmp_path: Path) -> None:
    """A 'running' row with a dead PID and ancient heartbeat is reclaimed
    by JobWorker.reclaim_stale() on startup."""
    db = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")

    template = Template(
        id="ghost", version=1, description="",
        nodes=[NodeSpec(id="ds", kind="downscale", params={})],
        outputs={"thumb": "ds.image"},
    )
    job = Job(
        template_id="ghost", template_version=1,
        inputs={
            "ds.image": Ref(node_hash="ext", port="image",
                            path=tmp_path / "stale.png",
                            type=PortType.IMAGE_PNG)
        },
    )

    import json as _json
    import sqlite3

    from server.catalog.db import migrate
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    with conn:
        conn.execute(
            """INSERT INTO jobs
               (id, status, template_id, template_version, template_json,
                job_json, submitted_at, started_at, worker_pid, heartbeat_at)
               VALUES ('ghost-1', 'running', ?, 1, ?, ?,
                       '2026-01-01T00:00:00+00:00',
                       '2026-01-01T00:00:00+00:00',
                       999999,
                       '2026-01-01T00:00:00+00:00')""",
            (
                template.id,
                _json.dumps(template.model_dump(mode="json")),
                _json.dumps(job.model_dump(mode="json")),
            ),
        )
    conn.close()

    worker = JobWorker(cache=cache, db_path=db)
    reclaimed = worker.reclaim_stale()
    assert reclaimed == 1

    mgr = JobManager(cache=cache, db_path=db)
    rec = mgr.get("ghost-1")
    assert rec is not None
    assert rec.status == "interrupted"
    assert rec.error
    types = [e.type for e in mgr.get_events("ghost-1")]
    assert "job_interrupted" in types


def test_event_extra_fields_round_trip(tmp_path: Path) -> None:
    """node_started events carry kind+hash; both must survive restart."""
    db = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")
    mgr = JobManager(cache=cache, db_path=db)
    template, job = _downscale(_png(tmp_path / "in.png"))
    with background_worker(cache, db):
        jid = mgr.submit(template, job)
        _wait(mgr, jid)

    mgr2 = JobManager(cache=cache, db_path=db)
    rec = mgr2.get(jid)
    assert rec is not None
    events = mgr2.get_events(jid)
    started = next(e for e in events if e.type == "node_started")
    # Extra payload included node kind and the cache hash.
    assert started.extra.get("kind") == "downscale"
    assert started.extra.get("hash")
