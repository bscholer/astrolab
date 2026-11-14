"""One object under two names (M 31 / NGC 224) becomes one target."""

from __future__ import annotations

from pathlib import Path

from server.catalog.db import open_db
from server.catalog.scanner import merge_duplicate_targets


def _target(db, tid, name, canonical, source="name", sep=0.0, sessions=()):
    db.execute(
        "INSERT INTO targets (id, name, resolved_canonical, resolved_source,"
        " resolved_separation_arcmin) VALUES (?, ?, ?, ?, ?)",
        (tid, name, canonical, source, sep),
    )
    for sid in sessions:
        db.execute("INSERT INTO sessions (id, target_id) VALUES (?, ?)", (sid, tid))


def test_merge(tmp_path: Path) -> None:
    with open_db(tmp_path / "c.sqlite") as db, db:
        _target(db, 9, "M 31", "NGC 224", sessions=(1, 2, 3))
        _target(db, 27, "NGC 224", "NGC 224", sessions=(4,))
        _target(db, 3, "C 20", "NGC 7000", sessions=(5, 6))
        _target(db, 15, "HD 200561", "NGC 7000", "position", 36.4, sessions=(7,))
        _target(db, 40, "Field near M 33", "NGC 598", "position", 20.0, sessions=(8,))
        _target(db, 41, "Far field", "NGC 7000", "position", 95.0, sessions=(9,))
        db.execute(
            "INSERT INTO target_masters (key, target_id, project_id, seen, created_at)"
            " VALUES ('9|DWARF3|TELE|Astro|60|1', 9, 'p', '[]', 0)"
        )

        merged = merge_duplicate_targets(db)

        assert sorted(merged) == [("HD 200561", "C 20"), ("M 31", "NGC 224")]
        owner = dict(db.execute("SELECT id, target_id FROM sessions").fetchall())
        assert [owner[s] for s in (1, 2, 3, 4)] == [27] * 4  # named after the object wins
        assert owner[7] == 3  # most sessions wins when no name matches
        assert owner[8] == 40 and owner[9] == 41  # position-only or too far: left alone
        assert [tuple(r) for r in db.execute("SELECT key, target_id FROM target_masters")] == [
            ("27|DWARF3|TELE|Astro|60|1", 27)
        ]
        assert merge_duplicate_targets(db) == []
