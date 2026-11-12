"""Fake scope: the yard decides frames, the API matches the relay, and nights run fast."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from astropy.io import fits
from fastapi.testclient import TestClient

from server.fakescope.app import create_app
from server.fakescope.core import FakeClock, FakeScope, alt_az_of
from server.fakescope.scene import Obstacle, Scene
from server.fakescope.sim import run_nights
from server.scope_plan import Candidate
from server.scope_service import ScopeServiceClient, ScopeServiceError

NIGHT = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # 11 pm in Bend
CLEAR = Scene(seed=1, clear_share=1.0, random_fail=0.0)
M31 = (10.6847, 41.2690)


def _scope(scene=CLEAR, at=NIGHT):
    clock = FakeClock(at)
    return FakeScope(scene, clock=clock, latency_scale=0), clock


def test_scene_skyline_and_weather():
    s = Scene()
    assert s.blocked(40, 0) and not s.blocked(55, 0)  # the house to the north
    assert s.blocked(5, 180) and not s.blocked(20, 180)  # only the fence to the south
    assert Scene(clear_share=0.0, overcast_share=1.0).cloudy(70, 180, NIGHT)
    kinds = {Scene(seed=n).night_kind(NIGHT) for n in range(20)}
    assert kinds == {"clear", "partly", "overcast"}


def test_capture_counts_frames_from_the_clock_and_the_yard_decides_them():
    scope, clock = _scope()
    scope.prepare()
    scope.calibrate()
    scope.goto("M 31", M31[0] / 15, M31[1])
    job = scope.start_capture(10, "15", 60, "astro")
    assert job["captured"] == 0
    clock.advance(17 * 4 + 1)
    assert scope.status()["job"]["captured"] == 4
    clock.advance(1000)
    st = scope.status()
    assert st["job"]["state"] == "done" and st["job"]["captured"] == 10 and st["job"]["failed"] == 0
    # A target behind the house fails every frame.
    house = Scene(
        seed=1,
        clear_share=1.0,
        random_fail=0.0,
        obstacles=(Obstacle("wall", 0, 359.9, 89, "house"),),
    )
    scope, clock = _scope(house)
    scope.goto("M 31", M31[0] / 15, M31[1])
    scope.start_capture(5, "15", 60, "astro")
    clock.advance(200)
    assert scope.status()["job"]["failed"] == 5


def test_sessions_hold_fits_files_with_real_headers():
    scope, clock = _scope()
    scope.goto("M 31", M31[0] / 15, M31[1])
    scope.start_capture(3, "30", 60, "duo")
    clock.advance(200)
    (session,) = [e["name"] for e in scope.list_files("")["entries"]]
    assert session.startswith("DWARF_RAW_TELE_M 31_EXP_30_GAIN_60_")
    files = scope.list_files(session)["entries"]
    assert len(files) == 3 and all(f["name"].endswith("_14C.fits") for f in files)
    first = scope.read_chunk(f"{session}/{files[0]['name']}", 0, 4 * 1024 * 1024)
    assert first["eof"] and first["size"] == files[0]["size"]
    import io

    hdr = fits.getheader(io.BytesIO(base64.b64decode(first["data"])))
    assert hdr["OBJECT"] == "M 31" and hdr["FILTER"] == "Duo-Band" and hdr["EXPTIME"] == 30
    assert hdr["TELESCOP"] == "DWARF 3"
    scope.delete_session(session)
    assert scope.list_files("")["entries"] == []


def test_refusals_match_the_relay():
    scope, clock = _scope()
    scope.goto("M 31", M31[0] / 15, M31[1])
    scope.start_capture(50, "15", 60, "astro")
    with pytest.raises(Exception) as busy:
        scope.start_capture(5, "15", 60, "astro")
    assert busy.value.code == "busy" and busy.value.status == 409
    noon = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)
    scope, clock = _scope(at=noon)
    with pytest.raises(Exception) as bright:
        scope.calibrate()
    assert "calibration failed" in str(bright.value)


def test_astrolab_client_drives_it_over_http():
    scope, clock = _scope()
    tc = TestClient(create_app(scope))

    def transport(method, url, headers, body, timeout=0):
        r = tc.request(method, url.removeprefix("http://fake"), content=body, headers=headers)
        return r.status_code, r.content

    c = ScopeServiceClient("http://fake", transport=transport)
    assert all(c.has(cap) for cap in ("status", "setup", "files", "preview", "pointing"))
    c.call("prepareScope")
    assert c.call("getStatus")["pitch"] == pytest.approx(172.5)
    c.call("gotoTarget", {"name": "M 31", "ra_hours": M31[0] / 15, "dec_degrees": M31[1]})
    c.call("startCapture", {"frames": 5, "exposure": "15", "gain": 60, "filter": "astro"})
    with pytest.raises(ScopeServiceError) as busy:
        c.call("startCapture", {"frames": 5, "exposure": "15", "gain": 60, "filter": "astro"})
    assert busy.value.code == "busy"
    frame = base64.b64decode(c.call("getFrame", {"camera": "wide"})["data"])
    assert frame[:2] == b"\xff\xd8"


def test_alt_az_formula_tracks_astropy():
    from server import sky

    loc = sky.observer_location(44.0, -121.3, 1100)
    for ra, dec in ((10.68, 41.27), (269.6, 66.6), (97.7, 5.0)):
        fast = alt_az_of(ra, dec, FakeScope(CLEAR, FakeClock(NIGHT)).loc, NIGHT)
        exact = sky.alt_az_now(ra, dec, loc, NIGHT)
        assert fast[0] == pytest.approx(exact[0], abs=1.0)


def test_a_clear_night_through_the_real_scheduler():
    targets = [Candidate("NGC 224", *M31, "Galaxy", goal_hours=20)]
    start = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)  # late afternoon, before dusk
    report = run_nights(CLEAR, targets, start, nights=0.6, tick_s=120)
    good = sum(n.good for n in report.nights)
    assert good > 1000, report.nights
    acc = report.skymap_accuracy()
    assert (
        acc.get("clear_right", 0) > 10
        and not acc.get("clear_wrong")
        and not acc.get("blocked_wrong")
    )


def test_the_sky_map_learns_a_tree_and_the_planner_then_skips_targets_behind_it():
    from server import sky
    from server.scope_plan import pick_target

    # A wall of tree to the east hides the Pleiades as it rises; M 31 stays clear.
    tree = (Obstacle("tree wall", 40, 140, 75),)
    scene = Scene(seed=2, clear_share=1.0, random_fail=0.0, obstacles=tree)
    pleiades = Candidate("M 45", 56.87, 24.12, goal_hours=50)
    targets = [pleiades, Candidate("NGC 224", *M31, goal_hours=50)]
    start = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
    report = run_nights(scene, targets, start, nights=3.4, tick_s=120)
    acc = report.skymap_accuracy()
    assert acc.get("blocked_right", 0) >= 1 and not acc.get("blocked_wrong"), acc

    # The next evening, find a target sitting in a patch learned as blocked. M 31 climbs
    # through the tree wall early each evening, which is where the failures were.
    loc = sky.observer_location(scene.lat, scene.lon, scene.elevation_m)
    t = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
    hit = None
    while hit is None:
        hit = next(
            (
                c
                for c in targets
                if report.skymap.blocked(*sky.alt_az_now(c.ra_deg, c.dec_deg, loc, t))
            ),
            None,
        )
        if hit is None:
            t += timedelta(minutes=5)
            assert t < datetime(2026, 11, 16, 13, 0, tzinfo=UTC), (
                "no target crossed a blocked patch"
            )
    assert pick_target([hit], loc, t, min_alt=0) is not None
    assert pick_target([hit], loc, t, min_alt=0, blocked=report.skymap.blocked) is None
