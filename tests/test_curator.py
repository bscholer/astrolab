"""Curator: session verdicts, the scrap plan, and purging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import curator
from server.catalog.db import open_db
from server.curator import decide, kept_folders, purge, purged_names, scrap_plan
from server.scope_cleanup import Cleaner
from server.scope_mirror import Mirror

NOW = 2_000_000_000.0
OLD = NOW - 2 * 3600  # settled


@pytest.fixture(autouse=True)
def _ten_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(curator, "SCRAP_BELOW_S", 600.0)


def _session(
    db, captures: Path, sid: int, *, folder: str, good: int, failed: int = 0,
    rescued: int | None = 0, exptime: float = 30.0, mtime: float = OLD,
) -> Path:
    d = captures / folder
    d.mkdir(parents=True, exist_ok=True)
    with db:
        db.execute("INSERT OR IGNORE INTO targets (id, name) VALUES (1, 'M 33')")
        db.execute(
            "INSERT INTO sessions (id, scope_id, target_id, exptime, frame_count, failed_count)"
            " VALUES (?, 'dwarf3', 1, ?, ?, ?)",
            (sid, exptime, good + failed, failed),
        )
        for i in range(good + failed):
            bad = i >= good
            p = d / f"{'failed_' if bad else ''}f{sid}_{i}.fits"
            p.write_bytes(b"x" * 100)
            cur = db.execute(
                "INSERT INTO frames (path, image_type, quality, mtime, rescued, size)"
                " VALUES (?, 'LIGHT', ?, ?, ?, 100)",
                (str(p), "failed" if bad else "ok", mtime, rescued if bad else None),
            )
            db.execute(
                "INSERT INTO session_frames (session_id, frame_id) VALUES (?, ?)",
                (sid, cur.lastrowid),
            )
    return d


def _verdicts(db) -> dict[int, str]:
    return dict(db.execute("SELECT session_id, verdict FROM session_verdicts").fetchall())


def test_verdicts(tmp_path: Path) -> None:
    c = tmp_path / "captures"
    with open_db(tmp_path / "c.sqlite") as db:
        _session(db, c, 1, folder="DWARF_RAW_long", good=20)  # 10 min good: keep now
        _session(db, c, 2, folder="DWARF_RAW_scraps", good=2, failed=18)  # 1 min: scrap
        _session(db, c, 3, folder="DWARF_RAW_saved", good=10, failed=12, rescued=1)  # 11 min
        _session(db, c, 4, folder="DWARF_RAW_live", good=2, mtime=NOW - 60)  # still going
        _session(db, c, 5, folder="DWARF_RAW_untriaged", good=2, failed=5, rescued=None)
        _session(db, c, 6, folder="DWARF_RAW_moon", good=3, exptime=0.004)  # not star work
        _session(db, c, 7, folder="DWARF_RAW_used", good=2)
        with db:
            db.execute(
                "INSERT INTO projects (id, name, template_id, template_version, template_json,"
                " base_job_json, current_seq, source_session_ids, created_at, updated_at)"
                " VALUES ('p', 'p', 't', 1, '{}', '{}', 0, ?, '', '')",
                (json.dumps(["7"]),),
            )

        assert decide(db, now=NOW) == {"keep": 4, "scrap": 1, "waiting": 2}
        assert _verdicts(db) == {1: "keep", 2: "scrap", 3: "keep", 6: "keep", 7: "keep"}
        assert kept_folders(db) == {
            str(c / f) for f in ("DWARF_RAW_long", "DWARF_RAW_saved", "DWARF_RAW_moon",
                                 "DWARF_RAW_used")
        }


def test_scrap_that_gains_frames_is_judged_again(tmp_path: Path) -> None:
    c = tmp_path / "captures"
    with open_db(tmp_path / "c.sqlite") as db:
        _session(db, c, 1, folder="DWARF_RAW_a", good=2)
        decide(db, now=NOW)
        assert _verdicts(db) == {1: "scrap"}
        # The scope came back to the target: 30 more good frames join the session.
        d = _session(db, c, 9, folder="DWARF_RAW_b", good=30, mtime=NOW + 10)
        with db:
            db.execute("UPDATE session_frames SET session_id = 1 WHERE session_id = 9")
            db.execute("DELETE FROM sessions WHERE id = 9")
        decide(db, now=NOW + 7200)
        assert _verdicts(db) == {1: "keep"}
        assert str(d) in kept_folders(db)


def test_purge_deletes_scrap_everywhere_and_remembers_it(tmp_path: Path) -> None:
    c, nas = tmp_path / "captures", tmp_path / "nas"
    with open_db(tmp_path / "c.sqlite") as db:
        _session(db, c, 1, folder="DWARF_RAW_long", good=30)
        scrap = _session(db, c, 2, folder="DWARF_RAW_scraps", good=2, failed=18)
        on_nas = nas / "captures" / "DWARF_RAW_scraps"
        on_nas.mkdir(parents=True)
        (on_nas / "f.fits").write_bytes(b"y" * 50)
        decide(db, now=NOW)

        plan = scrap_plan(db, c, nas)
        assert [(p.name, p.good, p.failed, p.bytes, p.nas_bytes) for p in plan] == [
            ("DWARF_RAW_scraps", 2, 18, 2000, 50)
        ]
        purge(db, plan[0])

        assert not scrap.exists() and not on_nas.exists()
        assert (c / "DWARF_RAW_long").exists()
        assert purged_names(db) == {"DWARF_RAW_scraps"}
        assert [r[0] for r in db.execute("SELECT id FROM sessions")] == [1]
        assert _verdicts(db) == {1: "keep"}
        assert scrap_plan(db, c, nas) == []


class _Scope:
    def __init__(self, sessions: dict[str, list[tuple[str, int]]]) -> None:
        self.sessions = sessions
        self.deleted: list[str] = []

    def has(self, _cap: str) -> bool:
        return True

    def call(self, op: str, args: dict | None = None) -> dict:
        if op == "listFiles":
            path = (args or {})["path"]
            if path == "":
                return {"entries": [{"name": n, "dir": True} for n in self.sessions]}
            return {"entries": [{"name": n, "dir": False, "size": s} for n, s in self.sessions[path]]}
        if op == "getStatus":
            return {"sd_free_gb": 5.0}
        if op == "deleteSession":
            self.deleted.append(args["session"])
            return {}
        raise AssertionError(op)


def test_mirror_skips_purged_and_cleaner_frees_them(tmp_path: Path) -> None:
    name = "DWARF_RAW_TELE_M 33_EXP_30_GAIN_60_2026-11-01-20-00-00-000"
    scope = _Scope({name: [("f.fits", 10)]})
    root = tmp_path / "captures"
    mirror = Mirror(
        lambda: scope, lambda: root, lambda: [], lambda _n: None, lambda: None,
        get_purged=lambda: {name},
    )
    assert mirror.run_once() == 0
    assert not (root / name).exists()

    cleaner = Cleaner(lambda: scope, lambda: root, get_purged=lambda: {name})
    assert cleaner.run_once() == [name]


def test_one_targets_sessions_in_a_night_count_together(tmp_path: Path) -> None:
    c = tmp_path / "captures"
    with open_db(tmp_path / "c.sqlite") as db:
        _session(db, c, 1, folder="DWARF_RAW_a", good=12)  # 6 min
        _session(db, c, 2, folder="DWARF_RAW_b", good=12)  # 6 min, same night: 12 min
        _session(db, c, 3, folder="DWARF_RAW_c", good=12)  # 6 min, a week later
        with db:
            db.execute("UPDATE sessions SET started_at = '2026-11-12T22:00:00' WHERE id = 1")
            db.execute("UPDATE sessions SET started_at = '2026-11-13T03:30:00' WHERE id = 2")
            db.execute("UPDATE sessions SET started_at = '2026-11-20T22:00:00' WHERE id = 3")
        decide(db, now=NOW)
        assert _verdicts(db) == {1: "keep", 2: "keep", 3: "scrap"}


def test_plan_skips_nested_folders(tmp_path: Path) -> None:
    c = tmp_path / "captures"
    with open_db(tmp_path / "c.sqlite") as db:
        _session(db, c, 1, folder="DWARF_RAW_MOSAIC_x/DWARF_RAW_panel", good=1)
        decide(db, now=NOW)
        assert _verdicts(db) == {1: "scrap"}
        assert scrap_plan(db, c, None) == []
