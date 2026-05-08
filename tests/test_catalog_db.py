"""Catalog DB schema + migration tests."""

from __future__ import annotations

from pathlib import Path

from server.catalog.db import CURRENT_SCHEMA_VERSION, _current_version, connect, migrate


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
