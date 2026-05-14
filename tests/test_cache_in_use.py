"""In-use marker tests for ContentCache and the eviction path.

Covers: marker creation/release, alive vs stale marker handling, and the
storage cleanup skipping entries held by a live job.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from server.cache import INUSE_PREFIX, ContentCache
from server.catalog.db import connect as open_catalog_db
from server.storage import purge_project_cache, run_cleanup


def _commit_entry(cache: ContentCache, h: str, payload_bytes: int = 1024) -> Path:
    """Reserve, write `data.bin` of N bytes, commit. Returns entry dir."""
    d = cache.reserve(h)
    (d / "data.bin").write_bytes(b"x" * payload_bytes)
    # Mimic commit() side effects without going through the manifest path
    # (these tests don't care about Refs, only the on-disk shape).
    (d / "_done").touch()
    (d / "_outputs.json").write_text("{}")
    return d


def test_mark_in_use_idempotent(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    _commit_entry(cache, "h1")
    cache.mark_in_use("h1", "job-A")
    cache.mark_in_use("h1", "job-A")  # idempotent
    cache.mark_in_use("h1", "job-B")
    assert cache.in_use_by("h1") == {"job-A", "job-B"}


def test_mark_in_use_missing_dir_is_noop(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    # No commit; just call mark on a hash whose dir doesn't exist.
    cache.mark_in_use("missing", "job-A")
    assert cache.in_use_by("missing") == set()


def test_release_job_marks_removes_all_for_job(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    _commit_entry(cache, "h1")
    _commit_entry(cache, "h2")
    cache.mark_in_use("h1", "job-A")
    cache.mark_in_use("h2", "job-A")
    cache.mark_in_use("h2", "job-B")

    removed = cache.release_job_marks("job-A")
    assert removed == 2
    assert cache.in_use_by("h1") == set()
    assert cache.in_use_by("h2") == {"job-B"}


def test_is_in_use_cleans_stale_markers(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    _commit_entry(cache, "h1")
    cache.mark_in_use("h1", "alive-job")
    cache.mark_in_use("h1", "dead-job")

    # dead-job is not in the alive set → marker should be reaped.
    assert cache.is_in_use("h1", {"alive-job"}) is True
    assert cache.in_use_by("h1") == {"alive-job"}

    # With no alive jobs and only the one remaining live marker stale,
    # it returns False and cleans the last marker too.
    assert cache.is_in_use("h1", set()) is False
    assert cache.in_use_by("h1") == set()


def _seed_jobs_table(db_path: Path, *jobs: tuple[str, str]) -> None:
    """Insert minimal job rows so build_reachability / _alive_job_ids find them.

    Each tuple is (job_id, status). Other columns get harmless filler.
    """
    open_catalog_db(db_path).close()  # ensure schema exists
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        for jid, status in jobs:
            conn.execute(
                """
                INSERT INTO jobs (id, status, template_id, template_version,
                                  template_json, job_json, submitted_at)
                VALUES (?, ?, 't', 1, '{}', '{}', '2026-01-01T00:00:00Z')
                """,
                (jid, status),
            )
    conn.close()


def test_run_cleanup_skips_live_in_use_entries(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    # Two big orphan entries (no owners → score 0 → first to evict).
    _commit_entry(cache, "h_locked", payload_bytes=10_000)
    _commit_entry(cache, "h_free", payload_bytes=10_000)
    # Mark h_locked as in use by a running job.
    _seed_jobs_table(db_path, ("live-job", "running"))
    cache.mark_in_use("h_locked", "live-job")

    # Force a sweep: budget below total so eviction runs.
    result = run_cleanup(cache, max_bytes=5_000, db_path=db_path)

    assert result.skipped_in_use_count == 1
    assert cache.entry_dir("h_locked").exists()  # protected
    assert not cache.entry_dir("h_free").exists()  # evicted


def test_run_cleanup_evicts_when_holder_is_terminal(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    _commit_entry(cache, "h_stale", payload_bytes=10_000)
    # Marker is held by a completed job — not load-bearing anymore.
    _seed_jobs_table(db_path, ("zombie-job", "completed"))
    cache.mark_in_use("h_stale", "zombie-job")

    result = run_cleanup(cache, max_bytes=0, db_path=db_path)

    assert result.skipped_in_use_count == 0
    assert result.evicted_count == 1
    assert not cache.entry_dir("h_stale").exists()


def test_purge_project_cache_skips_in_use(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    template_json = (
        '{"id":"t","version":1,"description":"d","nodes":[],"outputs":{}}'
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(
            """
            INSERT INTO projects (id, name, template_id, template_version,
                                  template_json, base_job_json, current_seq,
                                  source_session_ids, created_at, updated_at)
            VALUES ('p1', 'p1', 't', 1, ?, '{}', 0, '[]', ?, ?)
            """,
            (template_json, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (id, status, template_id, template_version,
                              template_json, job_json, submitted_at, node_hashes_json)
            VALUES ('j1', 'running', 't', 1, '{}', '{}', '2026-01-01T00:00:00Z', ?)
            """,
            ('{"n": "h_locked"}',),
        )
        conn.execute(
            """
            INSERT INTO project_history (project_id, seq, job_id, overrides_json,
                                         kind, created_at)
            VALUES ('p1', 1, 'j1', '{}', 'edit', '2026-01-01T00:00:00Z')
            """,
        )
    conn.close()

    _commit_entry(cache, "h_locked", payload_bytes=2_000)
    cache.mark_in_use("h_locked", "j1")

    evicted, _ = purge_project_cache(cache, "p1", db_path=db_path)

    assert evicted == 0  # the only candidate was locked
    assert cache.entry_dir("h_locked").exists()


def test_in_use_marker_naming(tmp_path: Path) -> None:
    """Guard the marker filename convention so storage tooling can grep
    for `_inuse_` predictably."""
    cache = ContentCache(root=tmp_path / "cache")
    _commit_entry(cache, "h1")
    cache.mark_in_use("h1", "job-abc")
    marker = cache.entry_dir("h1") / f"{INUSE_PREFIX}job-abc"
    assert marker.exists() and marker.is_file()
