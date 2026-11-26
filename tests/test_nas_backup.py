"""NAS backup: raw captures, finished images, and database snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from server.catalog.db import open_db
from server.nas_backup import NasBackup, copy_captures, copy_images, snapshot_db


def _old(path: Path) -> Path:
    os.utime(path, (1, 1))
    return path


def test_copy_captures_skips_failed_partial_and_fresh_files(tmp_path: Path) -> None:
    src = tmp_path / "captures" / "DWARF_RAW_TELE_M 31_EXP_15"
    src.mkdir(parents=True)
    (src / "M 31_0001.fits").write_bytes(b"good")
    (src / "failed_M 31_0002.fits").write_bytes(b"bad")
    (src / "M 31_0003.fits.part").write_bytes(b"half")
    (src / "M 31_0004.fits").write_bytes(b"still being mirrored")
    _old(src / "M 31_0001.fits")
    dest = tmp_path / "nas"

    assert copy_captures(tmp_path / "captures", dest) == 4
    assert sorted(p.name for p in (dest / src.name).iterdir()) == ["M 31_0001.fits"]
    assert copy_captures(tmp_path / "captures", dest) == 0  # same size: skipped


def test_copy_images_keeps_each_version_png_and_fits(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    for n in ("image.png", "stacked.fit", "recombined.fit", "stretched.fit"):
        (cache / n).write_bytes(n.encode())
    db = tmp_path / "a.sqlite"
    with open_db(db) as conn, conn:
        conn.execute(
            "INSERT INTO projects (id, name, template_id, template_version, template_json,"
            " base_job_json, current_seq, draft_mode, source_session_ids, created_at,"
            " updated_at) VALUES ('p', 'NGC 1499 / California', 't', 1, '{}', '{}', 0, 0,"
            " '[]', 'x', 'x')"
        )
        outputs = {
            p: {"path": str(cache / f), "node_hash": "h"}
            for p, f in [
                ("image", "image.png"),
                ("stacked", "stacked.fit"),
                ("recombined", "recombined.fit"),
                ("stretched", "stretched.fit"),
            ]
        }
        for seq, (jid, status) in enumerate([("j1", "completed"), ("j2", "failed")]):
            conn.execute(
                "INSERT INTO jobs (id, status, template_id, template_version, template_json,"
                " job_json, submitted_at, outputs_json) VALUES (?, ?, 't', 1, '{}', '{}', 'x', ?)",
                (jid, status, json.dumps(outputs)),
            )
            conn.execute(
                "INSERT INTO project_history (project_id, seq, job_id, overrides_json,"
                " created_at) VALUES ('p', ?, ?, '{}', 'x')",
                (seq, jid),
            )
    conn = sqlite3.connect(db)
    copy_images(conn, tmp_path / "nas")
    names = sorted(p.name for p in (tmp_path / "nas" / "NGC 1499 _ California").iterdir())
    assert names == [
        "NGC 1499 _ California v0 recombined.fit",
        "NGC 1499 _ California v0 stacked.fit",
        "NGC 1499 _ California v0.png",
    ]


def test_snapshot_db_daily_and_prunes_old(tmp_path: Path) -> None:
    db = tmp_path / "a.sqlite"
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.close()
    dest = tmp_path / "nas" / "db"
    dest.mkdir(parents=True)
    (dest / "astrolab-2026-01-01.sqlite").write_bytes(b"old")
    day = datetime(2026, 11, 13, tzinfo=UTC)

    assert snapshot_db(db, dest, day) is True
    assert snapshot_db(db, dest, day) is False
    assert [p.name for p in dest.iterdir()] == ["astrolab-2026-11-13.sqlite"]


def test_backup_is_off_without_a_root(tmp_path: Path) -> None:
    assert NasBackup(lambda: None, lambda: tmp_path, tmp_path / "a.sqlite").run() == {}


def test_copy_captures_takes_only_kept_scope_folders(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    for name in ("DWARF_RAW_TELE_M 31_kept", "DWARF_RAW_TELE_M 31_undecided", "CALI_FRAME"):
        (captures / name).mkdir(parents=True)
        (captures / name / "a.fits").write_bytes(b"x")
        _old(captures / name / "a.fits")
    dest = tmp_path / "nas"
    copy_captures(captures, dest, kept={str(captures / "DWARF_RAW_TELE_M 31_kept")})
    assert sorted(p.name for p in dest.iterdir()) == ["CALI_FRAME", "DWARF_RAW_TELE_M 31_kept"]


def test_copy_captures_keeps_rescued_failed_frames(tmp_path: Path) -> None:
    src = tmp_path / "captures" / "M 31"
    src.mkdir(parents=True)
    keep = src / "failed_M 31_0001.fits"
    keep.write_bytes(b"starry")
    (src / "failed_M 31_0002.fits").write_bytes(b"cloud")
    for f in src.iterdir():
        _old(f)
    dest = tmp_path / "nas"

    copy_captures(tmp_path / "captures", dest, rescued={str(keep)})
    assert [p.name for p in (dest / "M 31").iterdir()] == ["failed_M 31_0001.fits"]


def test_resolve_finds_outputs_after_the_cache_moved(tmp_path: Path) -> None:
    from server.nas_backup import _resolve

    (tmp_path / "cache" / "abc").mkdir(parents=True)
    (tmp_path / "cache" / "abc" / "image.png").write_bytes(b"png")
    ref = {"path": "/data/cache/abc/image.png", "node_hash": "abc"}
    assert _resolve(ref, tmp_path / "cache") == tmp_path / "cache" / "abc" / "image.png"
    assert _resolve({"path": "/gone/x.png", "node_hash": "zzz"}, tmp_path / "cache") is None


def test_layout_follows_env(tmp_path: Path, monkeypatch) -> None:
    from server.paths import nas_app, nas_captures

    assert nas_captures(tmp_path) == tmp_path / "captures"
    assert nas_app(tmp_path) == tmp_path
    monkeypatch.setenv("ASTROLAB_NAS_CAPTURES_DIR", "Astronomy")
    monkeypatch.setenv("ASTROLAB_NAS_APP_DIR", "astrolab")
    assert nas_captures(tmp_path) == tmp_path / "Astronomy"
    assert nas_app(tmp_path) == tmp_path / "astrolab"
