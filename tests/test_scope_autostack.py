"""AutoStacker: waits for quiet, stacks once per frame count, honors the setting."""

from __future__ import annotations

from server.scope_autostack import AutoStacker


def _make(enabled=True, found=(7, 40)):
    now = {"t": 0.0}
    submitted = []
    state = {"found": found}
    s = AutoStacker(
        lambda: enabled,
        lambda folder: state["found"],
        lambda sid: submitted.append(sid) or f"job{len(submitted)}",
        quiet_s=300,
        clock=lambda: now["t"],
    )
    return s, now, submitted, state


def test_waits_for_the_quiet_period():
    s, now, submitted, _ = _make()
    s.touch("DWARF_RAW_x")
    now["t"] = 299
    assert s.tick() == [] and not submitted
    now["t"] = 301
    assert s.tick() == ["job1"] and submitted == [7]


def test_new_frame_restarts_the_quiet_timer():
    s, now, submitted, _ = _make()
    s.touch("f")
    now["t"] = 200
    s.touch("f")
    now["t"] = 400
    assert s.tick() == []
    now["t"] = 501
    assert s.tick() == ["job1"]


def test_same_frame_count_is_not_stacked_twice_but_growth_is():
    s, now, submitted, state = _make()
    s.touch("f")
    now["t"] = 400
    s.tick()
    s.touch("f")
    now["t"] = 800
    assert s.tick() == []
    state["found"] = (7, 55)
    s.touch("f")
    now["t"] = 1200
    assert s.tick() == ["job2"]


def test_disabled_does_nothing_and_unscanned_folder_waits():
    s, now, submitted, _ = _make(enabled=False)
    s.touch("f")
    now["t"] = 999
    assert s.tick() == []
    s2, now2, sub2, _ = _make(found=None)
    s2.touch("f")
    now2["t"] = 999
    assert s2.tick() == [] and not sub2
    assert "f" in s2._last_arrival


def test_find_session_matches_the_mirrored_folder_and_counts_lights(tmp_path, monkeypatch):
    from server.api import _find_scope_session
    from server.catalog.db import connect

    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    conn = connect()
    conn.execute("INSERT INTO sessions (id) VALUES (1), (2)")
    rows = [
        ("/cap/DWARF_RAW_IC 1396_x/1.fits", "LIGHT", 1),
        ("/cap/DWARF_RAW_IC 1396_x/2.fits", "LIGHT", 1),
        ("/cap/DWARF_RAW_IC 1396_x/dark.fits", "DARK", 1),
        ("/cap/DWARF_RAW_other/1.fits", "LIGHT", 2),
    ]
    for path, kind, sid in rows:
        cur = conn.execute("INSERT INTO frames (path, image_type) VALUES (?,?)", (path, kind))
        conn.execute("INSERT INTO session_frames VALUES (?,?)", (sid, cur.lastrowid))
    conn.commit()
    conn.close()
    assert _find_scope_session("DWARF_RAW_IC 1396_x") == (1, 2)
    assert _find_scope_session("DWARF_RAW_missing") is None
