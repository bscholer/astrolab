"""Target masters and the morning summary, replaying a few nights."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server import masters
from server.catalog.db import open_db
from server.masters import apply, plan
from server.models import Job
from server.morning import morning
from server.recipes import recipe_for


def ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


@dataclass
class Entry:
    seq: int
    job_id: str
    label: str
    snapshot: dict | None = None


@dataclass
class FakeProject:
    id: str
    name: str
    source_session_ids: list[str]
    overrides: dict
    template: Any = None
    history: list[Entry] = field(default_factory=list)


class FakeProjects:
    def __init__(self) -> None:
        self.records: dict[str, FakeProject] = {}

    def get(self, pid: str) -> FakeProject | None:
        return self.records.get(pid)

    def create(self, *, name, template, base_job, source_session_ids, label, snapshot):
        p = FakeProject(f"p{len(self.records)}", name, source_session_ids,
                        dict(base_job.param_overrides), template)
        p.history.append(Entry(0, f"{p.id}-j0", label, snapshot))
        self.records[p.id] = p
        return p

    def swap_sessions(self, pid, *, new_base_job, new_session_ids, snapshot, label):
        p = self.records[pid]
        p.source_session_ids = new_session_ids
        p.history.append(Entry(len(p.history), f"{pid}-j{len(p.history)}", label, snapshot))
        return p, "job"


@pytest.fixture(autouse=True)
def _fake_build(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    """Real job builds need darks on disk; record the session lists instead."""
    built: list[list[int]] = []

    def build(conn, ids, template, calibration=None):
        built.append(list(ids))
        return Job(template_id="stack_per_night", template_version=1,
                   param_overrides={f"calibrate@{i}": {"dark_bins": []} for i in ids})

    monkeypatch.setattr(masters, "build_from_sessions", build)
    return built


def add_session(db, sid: int, *, target: str, start: str, frames: int = 40,
                exptime: float = 30.0, filt: str = "HaOIII", end_mtime: float | None = None,
                verdict: str | None = "keep") -> None:
    with db:
        db.execute("INSERT OR IGNORE INTO targets (name) VALUES (?)", (target,))
        tid = db.execute("SELECT id FROM targets WHERE name = ?", (target,)).fetchone()[0]
        db.execute(
            "INSERT INTO sessions (id, scope_id, target_id, instrument, camera, filter, exptime,"
            " gain, binning, started_at, frame_count, failed_count)"
            " VALUES (?, 'dwarf3', ?, 'DWARF 3', 'TELE', ?, ?, 60, 1, ?, ?, 0)",
            (sid, tid, filt, exptime, start, frames),
        )
        mtime = end_mtime if end_mtime is not None else ts(start) + 3600
        for i in range(frames):
            cur = db.execute(
                "INSERT INTO frames (path, image_type, quality, mtime) VALUES (?, 'LIGHT', 'ok', ?)",
                (f"/c/DWARF_RAW_{sid}/f{i}.fits", mtime),
            )
            db.execute("INSERT INTO session_frames VALUES (?, ?)", (sid, cur.lastrowid))
        if verdict:
            db.execute(
                "INSERT INTO session_verdicts VALUES (?, ?, ?, ?)",
                (sid, verdict, frames * exptime, mtime),
            )


def by_target(actions) -> dict[str, Any]:
    return {f"{a.target}/{a.filter}": a for a in actions}


def test_nights_join_one_master(tmp_path: Path, _fake_build) -> None:
    projects = FakeProjects()
    with open_db(tmp_path / "c.sqlite") as db:
        # Nov 12: two sessions of California at two exposures, both kept.
        add_session(db, 1, target="NGC 1499", start="2026-11-12T20:00:00", frames=120)
        add_session(db, 2, target="NGC 1499", start="2026-11-12T23:00:00", frames=60,
                    exptime=15.0)
        # The Moon gets no master; neither does a scrap session.
        add_session(db, 3, target="Moon", start="2026-11-12T21:00:00", exptime=0.004)
        add_session(db, 4, target="M 81", start="2026-11-12T22:00:00", verdict="scrap")

        now = ts("2026-11-13T09:00:00")
        [a] = plan(db, projects.get, now)
        assert (a.action, a.add, a.label) == ("create", [1, 2], "master: 1.2 h from Nov 12")
        pid = apply(db, projects, a, now)
        p = projects.get(pid)
        assert p.name == "NGC 1499"
        assert p.source_session_ids == ["1", "2"]
        # California is a nebula shot duo-band: the recipe rides on the first render.
        assert p.overrides["stretch"] == {"method": "auto"}
        assert p.overrides["color_balance"] == {"amount": 0.7}
        assert "calibrate@1" in p.overrides
        assert p.history[0].label == "master: 1.2 h from Nov 12, recipe: emission nebula, duo-band"
        assert plan(db, projects.get, now)[0].action == "up_to_date"

        # Nov 13: still shooting at 01:30, the night isn't settled.
        add_session(db, 5, target="NGC 1499", start="2026-11-13T20:00:00",
                    end_mtime=ts("2026-11-14T01:00:00"))
        assert plan(db, projects.get, ts("2026-11-14T01:30:00"))[0].action == "waiting"
        [a] = plan(db, projects.get, ts("2026-11-14T09:00:00"))
        assert (a.action, a.add, a.label) == ("add", [5], "+0.3 h from Nov 13")
        assert a.total_s == pytest.approx(4500 + 1200)
        apply(db, projects, a)
        assert _fake_build[-1] == [1, 2, 5]
        assert p.source_session_ids == ["1", "2", "5"]
        assert p.history[-1].snapshot == {"added": [5], "added_s": 1200.0, "total_s": 5700.0}

        # A night taken out by hand stays out.
        p.source_session_ids = ["1", "2"]
        assert plan(db, projects.get, ts("2026-11-14T09:00:00"))[0].action == "up_to_date"

        # A deleted master isn't rebuilt.
        del projects.records[pid]
        assert plan(db, projects.get, ts("2026-11-14T09:00:00"))[0].action == "retired"


def test_old_targets_and_filters(tmp_path: Path) -> None:
    projects = FakeProjects()
    with open_db(tmp_path / "c.sqlite") as db:
        add_session(db, 1, target="C 20", start="2025-09-11T21:00:00")
        add_session(db, 2, target="M 33", start="2026-11-11T21:00:00", filt="Astro")
        add_session(db, 3, target="M 33", start="2026-11-12T21:00:00", filt="HaOIII")
        now = ts("2026-11-14T09:00:00")
        actions = by_target(plan(db, projects.get, now))
        assert actions["C 20/HaOIII"].action == "too_old"
        assert actions["M 33/Astro"].action == "create"
        for key in ("M 33/Astro", "M 33/HaOIII"):
            apply(db, projects, actions[key], now)
        names = sorted(p.name for p in projects.records.values())
        assert names == ["M 33", "M 33 (HaOIII)"]
        astro = next(p for p in projects.records.values() if p.name == "M 33")
        assert astro.overrides["stretch"] == {"method": "auto"}
        assert "color_balance" not in astro.overrides


def test_recipes() -> None:
    assert recipe_for("Galaxy", "Astro").id == "galaxy"
    assert recipe_for("H II Region", "HaOIII").id == "nebula-duo"
    assert recipe_for("Supernova Remnant", "Astro").id == "nebula"
    assert recipe_for(None, None).id == "stars"
    assert recipe_for("Open Cluster", "Astro").overrides["graxpert_bg"] == {"enabled": True}


def test_morning(tmp_path: Path) -> None:
    projects = FakeProjects()
    jobs = {}

    def job(jid: str, h: str | None, status: str = "completed"):
        outputs = {"image": SimpleNamespace(node_hash=h)} if h else {}
        jobs[jid] = SimpleNamespace(status=status, outputs=outputs)

    with open_db(tmp_path / "c.sqlite") as db:
        add_session(db, 1, target="NGC 1499", start="2026-11-12T20:00:00", frames=120)
        pid = apply(db, projects, plan(db, projects.get, ts("2026-11-13T09:00:00"))[0])
        add_session(db, 2, target="NGC 1499", start="2026-11-13T20:00:00", frames=120)
        add_session(db, 3, target="M 81", start="2026-11-13T22:00:00", frames=10,
                    verdict="scrap")
        add_session(db, 4, target="M 45", start="2026-11-14T02:00:00", frames=10,
                    verdict=None, end_mtime=ts("2026-11-14T03:00:00"))
        apply(db, projects, plan(db, projects.get, ts("2026-11-14T09:00:00"))[0])
        job(f"{pid}-j0", "before")
        job(f"{pid}-j1", None, "running")

        m = morning(db, projects.get, jobs.get)
        assert m["night"] == "2026-11-13"
        t = {x["name"]: x for x in m["targets"]}
        assert [x["name"] for x in m["targets"]] == ["NGC 1499", "M 81", "M 45"]
        assert t["M 81"]["state"] == "scrap"
        assert t["M 45"]["state"] == "judging"
        cal = t["NGC 1499"]
        assert cal["state"] == "updated" and cal["kept_s"] == 3600
        assert (cal["shot_s"], cal["good_s"], cal["junk_s"]) == (3600, 3600, 0)
        assert m["totals"]["shot_s"] == 3600 + 300 + 300
        [u] = cal["masters"]
        assert u["status"] == "running" and u["after"] is None
        assert u["before"] == {"hash": "before", "port": "image"}
        assert u["noise_drop"] == pytest.approx(1 - 0.5 ** 0.5)
        assert json.dumps(m)

        # A later re-render of the master is what the card shows.
        p = projects.get(pid)
        p.history.append(Entry(2, f"{pid}-j2", "re-render"))
        job(f"{pid}-j2", "after")
        [u] = morning(db, projects.get, jobs.get)["targets"][0]["masters"]
        assert u["status"] == "completed" and u["after"] == {"hash": "after", "port": "image"}
        assert u["before"] == {"hash": "before", "port": "image"}

        # A render whose file left the cache is skipped, not shown broken.
        jobs[f"{pid}-j2"].outputs["image"].path = str(tmp_path / "gone.png")
        [u] = morning(db, projects.get, jobs.get)["targets"][0]["masters"]
        assert u["after"] is None and u["before"] == {"hash": "before", "port": "image"}

        # An earlier night can be asked for by date.
        assert morning(db, projects.get, jobs.get, "2026-11-12")["targets"][0]["state"] == "updated"


def test_auto_stretch_follows_the_image() -> None:
    import numpy as np

    from nodes.basic.stretch import _mtf, auto_mtf

    rng = np.random.default_rng(1)
    sky = rng.normal(0.002, 0.0002, (3, 400, 600)).astype(np.float32)

    # A moderate galaxy: the object lands at 0.4 with the sky inside its range.
    galaxy = sky.copy()
    galaxy[:, 150:250, 250:350] += 0.0015
    black, mid, info = auto_mtf(galaxy)
    assert info["object_out"] == pytest.approx(0.4, abs=0.01)
    assert 0.08 <= info["sky_out"] <= 0.15
    # A bright one would leave the sky below its floor; the floor lifts both.
    galaxy[:, 150:250, 250:350] += 0.004
    _, _, info = auto_mtf(galaxy)
    assert info["sky_out"] == pytest.approx(0.08, abs=0.002)
    assert info["object_out"] > 0.4
    assert black < info["sky"]

    # A faint nebula: the sky is lifted to its cap, not beyond.
    faint = sky.copy()
    faint[:, 100:300, 100:500] += 0.0002
    _, mid, info = auto_mtf(faint)
    assert info["sky_out"] == pytest.approx(0.15, abs=0.005)
    assert info["object_out"] > info["sky_out"]
    assert _mtf(0.5, 0.5) == pytest.approx(0.5)


def test_hoo_and_colour_strength(tmp_path: Path) -> None:
    import numpy as np
    from astropy.io import fits

    from nodes.basic.channel_map import hoo
    from nodes.basic.stretch import saturate_fits

    rng = np.random.default_rng(2)
    rgb = rng.normal(0.01, 0.0005, (3, 200, 200)).astype(np.float32)
    rgb[0, 50:150, 50:150] += 0.02  # H-alpha nebula, strong
    rgb[1:, 80:120, 80:120] += 0.004  # OIII core, faint
    rgb[:, :, :10] = 0.0  # registration border
    out = hoo(rgb, 1.0)
    assert np.array_equal(out[1], out[2])  # green and blue are both OIII
    assert np.allclose(out[0], rgb[0])  # red is H-alpha untouched
    core = out[1, 80:120, 80:120].mean() - np.median(out[1])
    assert core > 0.01  # faint OIII lifted toward H-alpha's spread
    assert (out[:, :, :10] == 0).all()  # border stays nodata
    weak = hoo(rgb, 0.5)
    assert weak[1, 80:120, 80:120].mean() < out[1, 80:120, 80:120].mean()

    p = tmp_path / "s.fit"
    img = np.stack([np.full((4, 4), 0.6), np.full((4, 4), 0.4), np.full((4, 4), 0.2)])
    fits.PrimaryHDU(img.astype(np.float32)).writeto(p)
    saturate_fits(p, 0.0)
    assert np.allclose(fits.getdata(p), 0.4)  # grey at the same brightness


def test_keeper_moves_masters_to_the_latest_template(tmp_path: Path) -> None:
    from server.masters import MasterKeeper
    from server.templates import load_template

    latest = load_template("stack_per_night")
    moved = []
    projects = FakeProjects()
    projects.upgrade_template = lambda pid, new_template: moved.append((pid, new_template.version))
    db_path = tmp_path / "c.sqlite"
    with open_db(db_path) as db:
        add_session(db, 1, target="M 33", start="2026-11-11T21:00:00", filt="Astro")
        pid = apply(db, projects, plan(db, projects.get, ts("2026-11-12T09:00:00"))[0])
    p = projects.get(pid)
    p.template = SimpleNamespace(id="stack_per_night", version=latest.version - 1)
    keeper = MasterKeeper(db_path, projects, lambda: True)
    assert keeper.upgrade() == [pid]
    assert moved == [(pid, latest.version)]
    p.template = latest
    assert keeper.upgrade() == []


def test_combine_trims_to_the_well_covered_area(tmp_path: Path) -> None:
    import numpy as np
    from astropy.io import fits

    from nodes.basic.combine_stacks import mask_low_coverage

    rng = np.random.default_rng(3)
    h, w = 1024, 1536
    noise = np.full((h, w), 0.001, dtype=np.float32)
    noise[:, :384] = noise[:, -384:] = 0.001 / np.sqrt(0.4)  # side bands: 40% of the exposure
    img = 0.02 + rng.normal(0, 1, (h, w)).astype(np.float32) * noise
    img[448:576, 704:832] += rng.normal(0, 0.01, (128, 128)).astype(np.float32)  # noisy core
    p = tmp_path / "c.fit"
    fits.PrimaryHDU(np.stack([img] * 3)).writeto(p)

    kept = mask_low_coverage(p, 0.7)
    out = fits.getdata(p)[0]
    assert 0.4 < kept < 0.6
    assert (out[:, :320] == 0).all() and (out[:, -320:] == 0).all()  # bands blanked
    assert (out[448:576, 704:832] != 0).all()  # the bright core stays
