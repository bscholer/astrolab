"""Mosaic planning, mosaic panels on the auto list, and the atlas coverage layer."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from server.catalog.db import default_db_path, open_db
from tests.test_sky_masters import TELE
from tests.test_target_masters import add_session
from tests.test_tonight_api import client  # noqa: F401  fixture

HEART = {"name": "IC 1805", "ra_deg": 38.2, "dec_deg": 61.45, "cols": 3, "rows": 2}


def test_plan_comes_back_with_the_panels(client: TestClient) -> None:  # noqa: F811
    r = client.put("/api/scope/mosaic", json=HEART)
    assert r.status_code == 200
    body = r.json()["plan"]
    assert [p["index"] for p in body["panels"]] == [1, 2, 3, 4, 5, 6]
    assert abs(body["width_deg"] - (2.95 + 2 * 2.95 * 0.8)) < 1e-6
    assert abs(body["height_deg"] - (1.67 + 1.67 * 0.8)) < 1e-6
    # Row 1 is the northern row.
    assert body["panels"][0]["dec_deg"] > body["panels"][3]["dec_deg"]


def test_plan_refuses_big_or_polar_grids(client: TestClient) -> None:  # noqa: F811
    assert client.put("/api/scope/mosaic", json={**HEART, "cols": 7}).status_code == 422
    polar = {**HEART, "dec_deg": 88.8, "cols": 2, "rows": 3}
    assert client.put("/api/scope/mosaic", json=polar).status_code == 400


def test_names_the_scope_can_store(client: TestClient) -> None:  # noqa: F811
    r = client.put("/api/scope/mosaic", json={**HEART, "name": "Heart/Soul_mix · 2", "cols": 2,
                                               "rows": 1})
    assert [t["name"] for t in r.json()["targets"]][-2:] == [
        "Heart Soul mix 2 panel 1 of 2",
        "Heart Soul mix 2 panel 2 of 2",
    ]
    assert client.put("/api/scope/mosaic", json={**HEART, "name": "··"}).status_code == 400


def test_mosaic_panels_join_and_leave_the_auto_list(client: TestClient) -> None:  # noqa: F811
    single = {"name": "M 31", "ra_deg": 10.68, "dec_deg": 41.27}
    r = client.put("/api/scope/auto", json={"enabled": False, "targets": [single]})
    assert r.status_code == 200
    req = {
        "name": "IC 1805",
        "ra_deg": 38.2,
        "dec_deg": 61.45,
        "cols": 2,
        "rows": 2,
        "object_type": "H II Region",
        "common_name": "Heart Nebula",
    }
    r = client.put("/api/scope/mosaic", json=req)
    assert r.status_code == 200
    targets = r.json()["targets"]
    assert [t["name"] for t in targets] == [
        "M 31",
        "IC 1805 panel 1 of 4",
        "IC 1805 panel 2 of 4",
        "IC 1805 panel 3 of 4",
        "IC 1805 panel 4 of 4",
    ]
    assert {t["mosaic"] for t in targets[1:]} == {"IC 1805"}
    assert targets[1]["object_type"] == "H II Region"

    # Asking again with another grid replaces the panels, not adds to them.
    r = client.put("/api/scope/mosaic", json={**req, "cols": 3, "rows": 1})
    names = [t["name"] for t in r.json()["targets"]]
    assert names == ["M 31", "IC 1805 panel 1 of 3", "IC 1805 panel 2 of 3", "IC 1805 panel 3 of 3"]

    r = client.delete("/api/scope/mosaic", params={"name": "IC 1805"})
    assert r.json() == {"targets": [single | {
        "object_type": None, "filter": None, "exposure_s": None, "gain": None,
        "goal_hours": None, "display_name": None, "common_name": None, "mosaic": None,
    }], "removed": 3}


def test_mosaic_needs_two_panels(client: TestClient) -> None:  # noqa: F811
    r = client.put(
        "/api/scope/mosaic", json={"name": "x", "ra_deg": 1, "dec_deg": 1, "cols": 1, "rows": 1}
    )
    assert r.status_code == 400


def test_coverage_lists_session_footprints(client: TestClient) -> None:  # noqa: F811
    with open_db(default_db_path()) as db:
        add_session(db, 1, target="IC 1805", start="2026-11-12T20:00:00", frames=20)
        add_session(db, 2, target="Moon", start="2026-11-12T21:00:00", exptime=0.004, frames=3)
        with db:
            db.execute(
                "UPDATE frames SET ra = 38.2, dec = 61.45, fits_headers = ? WHERE path LIKE ?",
                (json.dumps(TELE), "/c/DWARF_RAW_1/%"),
            )
    [entry] = client.get("/api/sky/coverage").json()
    assert entry["session_id"] == 1
    assert entry["target"] == "IC 1805"
    assert (entry["ra_deg"], entry["dec_deg"]) == (38.2, 61.45)
    assert abs(entry["width_deg"] - 2.946) < 0.01
    assert entry["usable_s"] == 600.0
    assert entry["night"] == "2026-11-12"
