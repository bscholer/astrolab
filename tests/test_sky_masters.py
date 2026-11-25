"""Masters group sessions by the sky they cover; mosaics plan their panels."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from server import coverage, masters
from server.catalog.db import open_db
from server.masters import apply, plan
from server.models import Job
from server.morning import morning
from tests.test_target_masters import FakeProjects, add_session, ts

TELE = {"FOCALLEN": 150.0, "XPIXSZ": 2.0, "YPIXSZ": 2.0, "NAXIS1": 3856, "NAXIS2": 2180}


@pytest.fixture(autouse=True)
def _fake_build(monkeypatch: pytest.MonkeyPatch) -> None:
    def build(conn, ids, template, calibration=None):
        return Job(template_id="stack_per_night", template_version=1, param_overrides={})

    monkeypatch.setattr(masters, "build_from_sessions", build)


def shoot(db, sid: int, target: str, ra: float, dec: float, start: str, **kw) -> None:
    """A kept session whose frames point at (ra, dec)."""
    add_session(db, sid, target=target, start=start, **kw)
    with db:
        db.execute(
            "UPDATE frames SET ra = ?, dec = ?, fits_headers = ? WHERE path LIKE ?",
            (ra, dec, json.dumps(TELE), f"/c/DWARF_RAW_{sid}/%"),
        )


def test_footprint_from_frames(tmp_path: Path) -> None:
    with open_db(tmp_path / "c.sqlite") as db:
        shoot(db, 1, "IC 1805", 38.2, 61.45, "2026-11-12T20:00:00")
        add_session(db, 2, target="Moon", start="2026-11-12T21:00:00", exptime=0.004)
        feet = coverage.footprints(db)
    assert set(feet) == {1}
    f = feet[1]
    assert (f.ra_deg, f.dec_deg) == (38.2, 61.45)
    assert f.width_deg == pytest.approx(2.946, abs=0.01)
    assert f.height_deg == pytest.approx(1.665, abs=0.01)


def test_median_ra_survives_the_wrap() -> None:
    assert coverage._median_ra([359.9, 0.1, 0.2]) == pytest.approx(0.1)


def test_overlap_needs_a_real_share() -> None:
    a = coverage.Footprint(100.0, 20.0, 2.95, 1.67)
    side_by_side = coverage.Footprint(100.0 + 2.5 / math.cos(math.radians(20)), 20.0, 2.95, 1.67)
    edge_to_edge = coverage.Footprint(100.0 + 2.9 / math.cos(math.radians(20)), 20.0, 2.95, 1.67)
    assert coverage.overlaps(a, side_by_side)
    assert not coverage.overlaps(a, edge_to_edge)
    # Across RA 0.
    assert coverage.overlaps(
        coverage.Footprint(359.5, 0.0, 2.95, 1.67), coverage.Footprint(0.5, 0.0, 2.95, 1.67)
    )


def test_a_mosaic_panel_joins_the_master_it_overlaps(tmp_path: Path) -> None:
    projects = FakeProjects()
    with open_db(tmp_path / "c.sqlite") as db:
        shoot(db, 1, "IC 1805", 38.2, 61.45, "2026-11-12T20:00:00", frames=120)
        [a] = plan(db, projects.get, ts("2026-11-13T09:00:00"))
        pid = apply(db, projects, a, ts("2026-11-13T09:00:00"))
        assert projects.get(pid).name == "IC 1805"

        # The next night a mosaic panel half a frame east of the Heart's centre.
        shoot(db, 2, "IC 1805 panel 2 of 4", 39.6, 61.45, "2026-11-13T20:00:00")
        # Something far away stays apart.
        shoot(db, 3, "NGC 7000", 314.7, 44.3, "2026-11-13T23:00:00")
        actions = {a.target: a for a in plan(db, projects.get, ts("2026-11-14T09:00:00"))}
        heart = actions["IC 1805"]
        assert (heart.action, heart.add, heart.project_id) == ("add", [2], pid)
        assert actions["NGC 7000"].action == "create"
        apply(db, projects, heart)
        assert projects.get(pid).source_session_ids == ["1", "2"]

        m = morning(db, projects.get, lambda _j: None, "2026-11-13")
        panel = next(t for t in m["targets"] if t["name"].startswith("IC 1805 panel"))
        assert panel["state"] == "updated"


def test_a_bridging_panel_merges_two_masters(tmp_path: Path) -> None:
    """Two targets 4.7 degrees apart get a master each; a mosaic panel between
    them joins both into the bigger one, and the smaller stays a plain project."""
    projects = FakeProjects()
    with open_db(tmp_path / "c.sqlite") as db:
        shoot(db, 1, "West", 100.0, 20.0, "2026-11-12T20:00:00", frames=200)
        shoot(db, 2, "East", 105.0, 20.0, "2026-11-12T23:00:00", frames=60)
        first = plan(db, projects.get, ts("2026-11-13T09:00:00"))
        assert sorted(a.action for a in first) == ["create", "create"]
        ids = {a.target: apply(db, projects, a, ts("2026-11-13T09:00:00")) for a in first}

        shoot(db, 3, "West panel 2 of 2", 102.5, 20.0, "2026-11-13T20:00:00")
        [a] = plan(db, projects.get, ts("2026-11-14T09:00:00"))
        assert a.action == "merge"
        assert a.project_id == ids["West"]
        assert a.add == [2, 3]
        assert a.label.startswith("joined East, +")
        apply(db, projects, a)
        assert projects.get(ids["West"]).source_session_ids == ["1", "2", "3"]
        # The smaller master is no longer kept, but its project stays.
        assert projects.get(ids["East"]) is not None
        keys = [r[0] for r in db.execute("SELECT key FROM target_masters")]
        assert keys == [a.key]
        assert plan(db, projects.get, ts("2026-11-14T09:00:00"))[0].action == "up_to_date"


def test_a_new_region_is_named_for_its_biggest_targets(tmp_path: Path) -> None:
    projects = FakeProjects()
    with open_db(tmp_path / "c.sqlite") as db:
        shoot(db, 1, "Heart panel 1 of 2", 100.0, 20.0, "2026-11-12T20:00:00", frames=50)
        shoot(db, 2, "Heart panel 2 of 2", 102.5, 20.0, "2026-11-12T21:00:00", frames=50)
        shoot(db, 3, "Soul", 105.0, 20.0, "2026-11-12T23:00:00", frames=60)
        [a] = plan(db, projects.get, ts("2026-11-13T09:00:00"))
        assert a.target == "Heart + Soul"
        pid = apply(db, projects, a, ts("2026-11-13T09:00:00"))
        assert projects.get(pid).name == "Heart + Soul"


def test_a_deleted_master_keeps_its_nights_out(tmp_path: Path) -> None:
    projects = FakeProjects()
    with open_db(tmp_path / "c.sqlite") as db:
        shoot(db, 1, "West", 100.0, 20.0, "2026-11-12T20:00:00")
        pid = apply(db, projects, plan(db, projects.get, ts("2026-11-13T09:00:00"))[0])
        del projects.records[pid]
        shoot(db, 2, "West panel 1 of 2", 101.0, 20.0, "2026-11-13T20:00:00")
        [a] = plan(db, projects.get, ts("2026-11-14T09:00:00"))
        assert a.action == "retired"


def test_filters_never_share_a_master(tmp_path: Path) -> None:
    projects = FakeProjects()
    with open_db(tmp_path / "c.sqlite") as db:
        shoot(db, 1, "M 33", 23.46, 30.66, "2026-11-12T20:00:00", filt="Astro")
        shoot(db, 2, "M 33", 23.46, 30.66, "2026-11-12T23:00:00", filt="HaOIII")
        actions = plan(db, projects.get, ts("2026-11-13T09:00:00"))
        assert sorted(a.filter for a in actions) == ["Astro", "HaOIII"]


def _sep(a: coverage.Panel, b: coverage.Panel) -> tuple[float, float]:
    fa = coverage.Footprint(a.ra_deg, a.dec_deg, 0, 0)
    fb = coverage.Footprint(b.ra_deg, b.dec_deg, 0, 0)
    return coverage.offset_deg(fa, fb)


@pytest.mark.parametrize("dec", [0.0, 45.0, 61.45, 80.0])
def test_mosaic_panels_step_by_the_frame_less_the_overlap(dec: float) -> None:
    panels = coverage.plan_mosaic(40.0, dec, 3, 2, overlap=0.2)
    assert [p.index for p in panels] == [1, 2, 3, 4, 5, 6]
    # Row 1 runs east to west, so the second panel sits west of the first.
    dx, dy = _sep(panels[0], panels[1])
    assert dx == pytest.approx(-2.95 * 0.8, abs=0.03)
    assert abs(dy) < 0.05
    dx, dy = _sep(panels[0], panels[3])
    assert dy == pytest.approx(-1.67 * 0.8, abs=0.03)
    # Neighbours overlap; the grid's centre is the requested centre.
    a, b = (coverage.Footprint(p.ra_deg, p.dec_deg, 2.95, 1.67) for p in panels[:2])
    assert coverage.overlaps(a, b)
    centre_ra = coverage._median_ra([p.ra_deg for p in panels])
    assert ((centre_ra - 40.0 + 180) % 360) - 180 == pytest.approx(0, abs=0.05)


def test_mosaic_rejects_an_empty_grid() -> None:
    with pytest.raises(ValueError):
        coverage.plan_mosaic(0, 0, 0, 2)
    with pytest.raises(ValueError):
        coverage.plan_mosaic(0, 0, 2, 2, overlap=0.95)
    with pytest.raises(ValueError):
        coverage.plan_mosaic(0, 88.5, 2, 3)


def test_panel_names_round_trip() -> None:
    name = coverage.panel_name("IC 1805", 3, 6)
    assert name == "IC 1805 panel 3 of 6"
    assert coverage.base_name(name) == "IC 1805"
    assert coverage.base_name("M 31") == "M 31"
