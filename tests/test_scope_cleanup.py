"""Cleanup deletes only sessions the mirror holds in full, and never while the scope shoots."""

from __future__ import annotations

from datetime import UTC, datetime

from server.scope_cleanup import Cleaner, mirrored_size, pick
from server.scope_service import ScopeServiceError

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
GB = 1_000_000_000


def _name(day: int, hour: int = 1) -> str:
    return f"DWARF_RAW_TELE_EXP_15_GAIN_60_2026-11-{day:02d}-{hour:02d}-00-00-000"


def test_pick_takes_old_sessions_only_while_space_is_fine():
    old, fresh = _name(5), _name(19)
    assert pick([(old, GB), (fresh, GB)], NOW, free_gb=80) == [old]


def test_pick_frees_the_oldest_sessions_first_when_space_runs_low_and_stops_at_the_target():
    sessions = [(_name(d), 10 * GB) for d in (15, 16, 17, 18)]
    assert pick(sessions, NOW, free_gb=15) == [_name(15), _name(16), _name(17)]


def test_pick_never_takes_a_session_that_may_still_be_shooting():
    just_now = "DWARF_RAW_TELE_EXP_15_GAIN_60_2026-11-20-11-30-00-000"
    assert pick([(just_now, 30 * GB)], NOW, free_gb=1) == []


def test_unknown_free_space_falls_back_to_the_age_rule():
    assert pick([(_name(18), 5 * GB)], NOW, free_gb=None) == []


class Client:
    def __init__(self, files, status=None, failing=None):
        self.files = files
        self.status = status or {"sd_free_gb": 5.0}
        self.deleted: list[str] = []
        self.failing = failing

    def has(self, capability):
        return True

    def call(self, op, payload=None, timeout=None):
        if op == "getStatus":
            return self.status
        if op == "listFiles":
            if payload["path"] == "":
                return {"entries": [{"name": s, "size": 0, "dir": True} for s in self.files]}
            return {
                "entries": [
                    {"name": n, "size": z, "dir": False}
                    for n, z in self.files[payload["path"]].items()
                ]
            }
        if op == "deleteSession":
            if self.failing:
                raise ScopeServiceError(self.failing)
            self.deleted.append(payload["session"])
        return {}


def _local(root, session, files):
    for name, size in files.items():
        path = root / session / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


def test_a_session_with_a_missing_or_short_local_file_is_not_mirrored(tmp_path):
    session = _name(5)
    remote = {"a.fits": 10, "b.fits": 20}
    client = Client({session: remote})
    _local(tmp_path, session, {"a.fits": 10, "b.fits": 19})
    assert mirrored_size(client, session, tmp_path) is None
    _local(tmp_path, session, {"b.fits": 20})
    assert mirrored_size(client, session, tmp_path) == 30


def test_cleaner_deletes_the_mirrored_old_session_and_keeps_the_unmirrored_one(tmp_path):
    mirrored, unmirrored = _name(5), _name(6)
    files = {mirrored: {"a.fits": 10}, unmirrored: {"a.fits": 10}}
    _local(tmp_path, mirrored, {"a.fits": 10})
    client = Client(files, {"sd_free_gb": 80.0})
    cleaner = Cleaner(lambda: client, lambda: tmp_path, clock=lambda: NOW)
    assert cleaner.run_once() == [mirrored]
    assert client.deleted == [mirrored]


def test_cleaner_does_nothing_while_the_scope_is_capturing(tmp_path):
    session = _name(5)
    _local(tmp_path, session, {"a.fits": 10})
    client = Client({session: {"a.fits": 10}}, {"job": {"state": "running"}, "sd_free_gb": 1.0})
    assert Cleaner(lambda: client, lambda: tmp_path, clock=lambda: NOW).run_once() == []
    assert not client.deleted


def test_cleaner_reports_a_refused_delete_and_does_not_raise(tmp_path):
    session = _name(5)
    _local(tmp_path, session, {"a.fits": 10})
    client = Client({session: {"a.fits": 10}}, {"sd_free_gb": 80.0}, failing="a capture is running")
    cleaner = Cleaner(lambda: client, lambda: tmp_path, clock=lambda: NOW)
    assert cleaner.run_once() == []
    assert "capture is running" in cleaner.last["error"]
