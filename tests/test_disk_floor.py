"""Jobs leave a share of the cache disk free, so they can't fill the node.

The cache shares lambda-ubuntu's /scratch SSD with containerd's images. A
Veil stack once wrote 341 GB there and kubelet evicted every pod on the
node. The floor holds back space in the per-node preflight, and the job
monitor stops a job whose writes reach it anyway.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from server import jobs as jobs_mod
from server.cache import ContentCache
from server.catalog.db import connect as open_catalog_db
from server.jobs import JobRecord, JobWorker
from server.models import Job, NodeSpec, Template
from server.runtime import JobCancelled
from server.storage import (
    InsufficientStorageError,
    disk_floor_bytes,
    ensure_disk_headroom,
)


def _disk(total: int, free: int) -> object:
    return type("D", (), {"total_bytes": total, "used_bytes": total - free, "free_bytes": free})


def test_floor_is_a_share_of_the_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("server.storage._disk_usage", return_value=_disk(10**6, 10**6)):
        monkeypatch.delenv("ASTROLAB_DISK_FLOOR_FRACTION")
        assert disk_floor_bytes(tmp_path) == 200_000
        monkeypatch.setenv("ASTROLAB_DISK_FLOOR_FRACTION", "0.3")
        assert disk_floor_bytes(tmp_path) == 300_000
        monkeypatch.setenv("ASTROLAB_DISK_FLOOR_FRACTION", "0")
        assert disk_floor_bytes(tmp_path) == 0


def test_preflight_never_hands_out_the_floor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASTROLAB_DISK_FLOOR_FRACTION", "0.2")
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    # 300 KB free of 1 MB: 200 KB is floor, so 100 KB is usable.
    with patch("server.storage._disk_usage", return_value=_disk(10**6, 300_000)):
        assert ensure_disk_headroom(
            cache, need_bytes=50_000, cache_max_bytes=10**9, db_path=db_path
        ) is None
        with pytest.raises(InsufficientStorageError) as excinfo:
            ensure_disk_headroom(
                cache, need_bytes=150_000, cache_max_bytes=10**9, db_path=db_path
            )
    assert excinfo.value.floor_bytes == 200_000
    assert "must stay free" in str(excinfo.value)


def _queued(db_path: Path, job_id: str) -> JobRecord:
    template = Template(
        id="t", version=1, description="",
        nodes=[NodeSpec(id="n", kind="downscale", params={})],
        outputs={"image": "n.image"},
    )
    record = JobRecord(
        id=job_id, status="running", template=template,
        job=Job(template_id="t", template_version=1),
        submitted_at="2026-01-01T00:00:00Z",
    )
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO jobs (id, status, template_id, template_version,
                              template_json, job_json, submitted_at)
            VALUES (?, 'running', 't', 1, ?, '{}', '2026-01-01T00:00:00Z')
            """,
            (job_id, json.dumps(template.model_dump(mode="json"))),
        )
    conn.close()
    return record


def _status(db_path: Path, job_id: str) -> tuple[str, str | None]:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return row[0], row[1]


@pytest.fixture
def full_disk(monkeypatch: pytest.MonkeyPatch):
    """The cache disk is under its floor and nothing can be evicted."""
    monkeypatch.setattr(JobWorker, "CANCEL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(JobWorker, "DISK_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(jobs_mod, "disk_floor_bytes", lambda _p: 100)
    monkeypatch.setattr(jobs_mod, "disk_free_bytes", lambda _p: 10)

    def no_room(*_a, **_k):
        raise InsufficientStorageError(need_bytes=1, free_bytes=10, evicted_bytes=0, floor_bytes=100)

    monkeypatch.setattr(jobs_mod, "ensure_disk_headroom", no_room)


def test_job_stops_when_the_disk_reaches_its_floor(tmp_path: Path, monkeypatch, full_disk) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    worker = JobWorker(cache=cache, db_path=db_path)
    monkeypatch.setattr(worker, "sweep_cache", lambda: None)

    def writing_run_job(*_a, cancel: threading.Event, **_k) -> dict:
        # A node that honours cancel, as Siril steps do.
        if cancel.wait(5):
            raise JobCancelled("n")
        return {}

    monkeypatch.setattr(jobs_mod, "run_job", writing_run_job)
    started = time.monotonic()
    worker._run(_queued(db_path, "job-disk"))

    assert time.monotonic() - started < 2
    status, error = _status(db_path, "job-disk")
    assert status == "failed"
    assert error is not None and "cache disk" in error


def test_a_job_that_ignores_the_stop_loses_its_partial_entries(
    tmp_path: Path, monkeypatch, full_disk
) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    worker = JobWorker(cache=cache, db_path=db_path)
    monkeypatch.setattr(worker, "sweep_cache", lambda: None)
    monkeypatch.setattr(JobWorker, "DISK_KILL_GRACE_SECONDS", 0.05)
    exited = threading.Event()
    monkeypatch.setattr(jobs_mod.os, "_exit", lambda _code: exited.set())

    done = cache.reserve("hdone")
    (done / "_done").touch()
    partial = cache.reserve("hpartial")
    (partial / "data.bin").write_bytes(b"x" * 100)
    record = _queued(db_path, "job-stuck")
    record.node_hashes.update(a="hdone", b="hpartial")

    at_exit: list[str] = []

    def stubborn_run_job(*_a, **_k) -> dict:
        exited.wait(5)  # a node that never looks at cancel
        # The real os._exit never returns; read the row as the worker left it.
        at_exit.append(_status(db_path, "job-stuck")[0])
        return {}

    monkeypatch.setattr(jobs_mod, "run_job", stubborn_run_job)
    worker._run(record)

    assert exited.is_set()
    assert cache.entry_dir("hdone").exists()
    assert not cache.entry_dir("hpartial").exists()
    assert at_exit == ["failed"]
