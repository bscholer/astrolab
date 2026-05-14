"""Tests for the JobWorker auto cache sweep.

The worker runs `run_cleanup` once before every job and again periodically
from its monitor thread, using `cache_max_bytes` from settings. Without
this wiring the cache was unbounded — Veil Nebula sat at 1.5TB on the
Linux box because nothing ever called cleanup.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from server.cache import ContentCache
from server.catalog.db import connect as open_catalog_db
from server.jobs import JobWorker
from server.storage import SETTING_CACHE_MAX_BYTES, set_setting


def _commit_entry(cache: ContentCache, h: str, payload: int = 1024) -> Path:
    d = cache.reserve(h)
    (d / "data.bin").write_bytes(b"x" * payload)
    (d / "_done").touch()
    (d / "_outputs.json").write_text("{}")
    return d


def test_sweep_cache_evicts_to_configured_budget(tmp_path: Path) -> None:
    """sweep_cache reads cache_max_bytes from settings and evicts down to it."""
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    _commit_entry(cache, "h1", payload=10_000)
    _commit_entry(cache, "h2", payload=10_000)
    set_setting(SETTING_CACHE_MAX_BYTES, 5_000, db_path=db_path)

    worker = JobWorker(cache=cache, db_path=db_path)
    worker.sweep_cache()

    # Both were orphans (no project owns them), both ~10KB; with a 5KB
    # budget the sweep must drop both to comply.
    assert not cache.entry_dir("h1").exists()
    assert not cache.entry_dir("h2").exists()


def test_sweep_cache_noops_under_budget(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    _commit_entry(cache, "h1", payload=1_000)
    # Budget high enough to keep everything.
    set_setting(SETTING_CACHE_MAX_BYTES, 1_000_000_000, db_path=db_path)

    worker = JobWorker(cache=cache, db_path=db_path)
    worker.sweep_cache()

    assert cache.entry_dir("h1").exists()


def test_sweep_cache_swallows_errors(tmp_path: Path) -> None:
    """A broken cleanup must not raise — it can't fail the job it's protecting."""
    cache = ContentCache(root=tmp_path / "cache")
    # No db, no migration: get_setting / run_cleanup will hit an unmigrated
    # SQLite path. sweep_cache should log and move on, not raise.
    worker = JobWorker(cache=cache, db_path=tmp_path / "does-not-exist.sqlite")
    worker.sweep_cache()  # must not raise


def test_pre_job_sweep_runs_before_run_job(tmp_path: Path, monkeypatch) -> None:
    """JobWorker._run sweeps the cache before invoking run_job."""
    from server import jobs as jobs_mod

    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()

    order: list[str] = []

    worker = JobWorker(cache=cache, db_path=db_path)

    def fake_sweep() -> None:
        order.append("sweep")

    def fake_run_job(*args, **kwargs) -> dict:
        order.append("run_job")
        return {}

    monkeypatch.setattr(worker, "sweep_cache", fake_sweep)
    monkeypatch.setattr(jobs_mod, "run_job", fake_run_job)

    # Hand-roll a minimal queued row + record so we don't need a real template.
    import json as _json

    from server.jobs import JobRecord
    from server.models import Job, NodeSpec, Template

    template = Template(
        id="t", version=1, description="",
        nodes=[NodeSpec(id="n", kind="downscale", params={})],
        outputs={"image": "n.image"},
    )
    record = JobRecord(
        id="job-1", status="running",
        template=template,
        job=Job(template_id="t", template_version=1),
        submitted_at="2026-01-01T00:00:00Z",
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(
            """
            INSERT INTO jobs (id, status, template_id, template_version,
                              template_json, job_json, submitted_at)
            VALUES (?, 'running', 't', 1, ?, '{}', '2026-01-01T00:00:00Z')
            """,
            (record.id, _json.dumps(template.model_dump(mode="json"))),
        )
    conn.close()

    worker._run(record)

    assert order[:2] == ["sweep", "run_job"], (
        f"expected sweep before run_job, got {order}"
    )


def test_monitor_periodic_sweep(tmp_path: Path, monkeypatch) -> None:
    """The monitor thread sweeps periodically while the job runs."""
    from server import jobs as jobs_mod

    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()

    worker = JobWorker(cache=cache, db_path=db_path)
    # Force the monitor to sweep on every tick.
    monkeypatch.setattr(JobWorker, "CACHE_SWEEP_SECONDS", 0)
    monkeypatch.setattr(JobWorker, "CANCEL_POLL_SECONDS", 0.01)

    sweep_calls: list[float] = []

    def fake_sweep() -> None:
        sweep_calls.append(time.monotonic())

    def slow_run_job(*args, **kwargs) -> dict:
        # Sleep long enough for the monitor to tick a few times.
        time.sleep(0.1)
        return {}

    monkeypatch.setattr(worker, "sweep_cache", fake_sweep)
    monkeypatch.setattr(jobs_mod, "run_job", slow_run_job)

    import json as _json

    from server.jobs import JobRecord
    from server.models import Job, NodeSpec, Template

    template = Template(
        id="t", version=1, description="",
        nodes=[NodeSpec(id="n", kind="downscale", params={})],
        outputs={"image": "n.image"},
    )
    record = JobRecord(
        id="job-2", status="running",
        template=template,
        job=Job(template_id="t", template_version=1),
        submitted_at="2026-01-01T00:00:00Z",
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(
            """
            INSERT INTO jobs (id, status, template_id, template_version,
                              template_json, job_json, submitted_at)
            VALUES (?, 'running', 't', 1, ?, '{}', '2026-01-01T00:00:00Z')
            """,
            (record.id, _json.dumps(template.model_dump(mode="json"))),
        )
    conn.close()

    worker._run(record)

    # 1 pre-job + at least 1 from the monitor thread during slow_run_job.
    assert len(sweep_calls) >= 2, (
        f"expected pre-job + periodic sweep, got {len(sweep_calls)}"
    )
