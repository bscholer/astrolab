"""Catalog DB schema + migration tests."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from server.catalog.db import (
    CURRENT_SCHEMA_VERSION,
    _current_version,
    connect,
    migrate,
    retry_on_locked,
)


def test_connect_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "cat.sqlite"
    conn = connect(db_path)
    try:
        assert _current_version(conn) == CURRENT_SCHEMA_VERSION
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for required in {"frames", "sessions", "session_frames", "targets", "schema_version"}:
            assert required in tables
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "cat.sqlite"
    conn = connect(db_path)
    try:
        migrate(conn)
        migrate(conn)
        assert _current_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_targets_unique_name(tmp_path: Path) -> None:
    conn = connect(tmp_path / "cat.sqlite")
    try:
        conn.execute("INSERT INTO targets (name) VALUES (?)", ("M 33",))
        try:
            conn.execute("INSERT INTO targets (name) VALUES (?)", ("M 33",))
        except Exception:
            return
        raise AssertionError("expected UNIQUE constraint on targets.name")
    finally:
        conn.close()


def test_frames_path_unique(tmp_path: Path) -> None:
    conn = connect(tmp_path / "cat.sqlite")
    try:
        conn.execute(
            "INSERT INTO frames (path, image_type, scope_id) VALUES (?,?,?)",
            ("/tmp/a.fits", "LIGHT", "dwarf3"),
        )
        try:
            conn.execute(
                "INSERT INTO frames (path, image_type, scope_id) VALUES (?,?,?)",
                ("/tmp/a.fits", "LIGHT", "dwarf3"),
            )
        except Exception:
            return
        raise AssertionError("expected UNIQUE constraint on frames.path")
    finally:
        conn.close()


def test_retry_on_locked_retries_and_succeeds() -> None:
    """retry_on_locked re-invokes the callable on 'database is locked'.

    Real symptom: a single calibrate progress event hitting SQLITE_BUSY would
    bubble out of ctx.progress and fail the whole pipeline run. The decorator
    sits on the write methods so the transient lock is absorbed instead.
    """
    calls: list[int] = []

    @retry_on_locked
    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3


def test_retry_on_locked_passes_through_other_operational_errors() -> None:
    """Only locked/busy variants retry; schema mismatches and the like must
    surface immediately so they don't get retried forever and so the caller
    sees the real failure."""

    @retry_on_locked
    def boom() -> None:
        raise sqlite3.OperationalError("no such table: frames")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        boom()


def test_retry_on_locked_gives_up_after_max_attempts() -> None:
    """If every attempt fails with locked, eventually the error is raised so
    the caller can fail visibly instead of looping forever."""
    calls: list[int] = []

    @retry_on_locked
    def always_locked() -> None:
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        always_locked()
    assert len(calls) == 8


def test_retry_on_locked_unblocks_when_lock_released(tmp_path: Path) -> None:
    """End-to-end smoke: a writer holding the DB briefly should not break a
    second writer wrapped in retry_on_locked, because the retry waits the
    holder out. Sets busy_timeout=0 on the contending connection so we don't
    rely on the 10s default to expose the race deterministically."""
    db = tmp_path / "lock.sqlite"
    conn = connect(db)
    try:
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.commit()
    finally:
        conn.close()

    holder_started = threading.Event()
    holder_release = threading.Event()

    def holder() -> None:
        c = sqlite3.connect(db)
        c.execute("PRAGMA busy_timeout = 0")
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO t VALUES (1)")
        holder_started.set()
        holder_release.wait(timeout=2.0)
        c.commit()
        c.close()

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    holder_started.wait(timeout=2.0)

    @retry_on_locked
    def writer() -> int:
        c = sqlite3.connect(db)
        c.execute("PRAGMA busy_timeout = 50")
        try:
            c.execute("INSERT INTO t VALUES (2)")
            c.commit()
            return 1
        finally:
            c.close()

    def release_soon() -> None:
        time.sleep(0.15)
        holder_release.set()

    threading.Thread(target=release_soon, daemon=True).start()
    assert writer() == 1
    t.join(timeout=2.0)
