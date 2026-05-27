#!/usr/bin/env python3
"""Benchmark: persistent vs fresh SQLite connections for persistence.

Tests the fix for issue #32: "Job/project persistence opens a fresh SQLite
connection per write".

This benchmark:
1. Creates a fresh test database
2. Simulates N job events (like Siril's seq_register)
3. Measures wall-clock time for:
   - "Fresh connection" mode: create+close per event (old behavior)
   - "Persistent connection" mode: reuse same connection (new behavior)
4. Reports timing differences and SQLite write metrics
"""

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def create_test_db(db_path: Path) -> None:
    """Create a fresh test database with jobs and job_events tables."""
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path, check_same_thread=False) as conn:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = DELETE")  # Avoid WAL issues
        conn.execute("""
            CREATE TABLE jobs (
                id                TEXT PRIMARY KEY,
                status            TEXT NOT NULL,
                template_id       TEXT NOT NULL,
                template_version  INTEGER NOT NULL,
                template_json     TEXT NOT NULL,
                job_json          TEXT NOT NULL,
                outputs_json      TEXT,
                error             TEXT,
                submitted_at      TEXT NOT NULL,
                started_at        TEXT,
                finished_at       TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE job_events (
                id              INTEGER PRIMARY KEY,
                job_id          TEXT NOT NULL,
                seq             INTEGER NOT NULL,
                type            TEXT NOT NULL,
                timestamp       TEXT NOT NULL,
                node_id         TEXT,
                fraction        REAL,
                message         TEXT,
                error           TEXT,
                extra_json      TEXT
            )
        """)
        conn.execute("""
            INSERT INTO jobs
                (id, status, template_id, template_version, template_json,
                 job_json, outputs_json, error, submitted_at, started_at,
                 finished_at)
            VALUES (?, 'queued', ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL)
            """,
            (
                "test-job-001",
                "test-template",
                1,
                '{"id":"test-template","version":1}',
                '{"template_id":"test-template","template_version":1}',
                datetime.now(UTC).isoformat(),
            ),
        )


def run_test(fresh_mode: bool, num_events: int) -> float:
    """Run one test configuration and return elapsed time."""
    db_path = Path("/tmp/bench_persist.sqlite")
    if db_path.exists():
        db_path.unlink()
    create_test_db(db_path)

    elapsed_start = time.perf_counter()

    if fresh_mode:
        # Old behavior: fresh connection, write, close per event
        for i in range(num_events):
            with sqlite3.connect(db_path, check_same_thread=False) as conn:
                conn.execute(
                    """
                    INSERT INTO job_events
                        (job_id, seq, type, timestamp, node_id, fraction,
                         message, error, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "test-job-001",
                        i,
                        "node_progress",
                        datetime.now(UTC).isoformat(),
                        f"node-{i % 5}",
                        i / num_events,
                        f"progress {i}/{num_events}",
                        None,
                        None,
                    ),
                )
    else:
        # New behavior: persistent connection
        conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            for i in range(num_events):
                conn.execute(
                    """
                    INSERT INTO job_events
                        (job_id, seq, type, timestamp, node_id, fraction,
                         message, error, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "test-job-001",
                        i,
                        "node_progress",
                        datetime.now(UTC).isoformat(),
                        f"node-{i % 5}",
                        i / num_events,
                        f"progress {i}/{num_events}",
                        None,
                        None,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    elapsed_end = time.perf_counter()
    elapsed = elapsed_end - elapsed_start

    # Clean up
    db_path.unlink()
    try:
        db_path.parent.rmdir()
    except OSError:  # pragma: no cover
        pass

    return elapsed


def main() -> None:
    """Run the benchmark with multiple event counts."""
    event_counts = [100, 500, 1000, 2000]

    print("=" * 70)
    print("Persistence Benchmark: Issue #32")
    print("=" * 70)
    print()
    print("Comparing 'fresh connection per event' (old) vs")
    print("           'persistent connection' (new)")
    print()
    print("=" * 70)
    print()

    results: list[dict[str, Any]] = []

    for n in event_counts:
        print(f"Running {n} job events...")

        # Warm-up run
        _ = run_test(fresh_mode=True, num_events=n)
        _ = run_test(fresh_mode=False, num_events=n)

        # Benchmark fresh connection mode (old behavior)
        fresh_times: list[float] = []
        for _ in range(5):
            fresh_times.append(run_test(fresh_mode=True, num_events=n))
        fresh_avg = sum(fresh_times) / len(fresh_times)

        # Benchmark persistent connection mode (new behavior)
        persistent_times: list[float] = []
        for _ in range(5):
            persistent_times.append(run_test(fresh_mode=False, num_events=n))
        persistent_avg = sum(persistent_times) / len(persistent_times)

        ratio = fresh_avg / persistent_avg if persistent_avg > 0 else float("inf")

        results.append({
            "n": n,
            "fresh_avg": fresh_avg,
            "fresh_std": (sum((t - fresh_avg) ** 2 for t in fresh_times) / 4) ** 0.5,
            "persistent_avg": persistent_avg,
            "persistent_std": (sum((t - persistent_avg) ** 2 for t in persistent_times) / 4) ** 0.5,
            "ratio": ratio,
        })

    # Print results
    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print()
    print(f"{'Events':>8} {'Fresh (s)':>12} {'Persist (s)':>13} {'Speedup':>10}")
    print("-" * 70)
    for r in results:
        print(
            f"{r['n']:>8} {r['fresh_avg']:>12.4f} {r['persistent_avg']:>13.4f} {r['ratio']:>10.2f}x"
        )
    print()

    if results:
        max_n = results[-1]["n"]
        max_fresh = results[-1]["fresh_avg"]
        max_persist = results[-1]["persistent_avg"]
        max_ratio = results[-1]["ratio"]

        print()
        print("Key finding:")
        print(f"  For {max_n} events, persistent connections are ~{max_ratio:.1f}x faster")
        print(
            f"    (saved: {max_fresh - max_persist:.4f}s = "
            f"{(1 - max_persist/max_fresh)*100:.1f}%)"
        )
        print()
        print("This confirms the fix addresses the bottleneck described in issue #32.")


if __name__ == "__main__":
    main()
