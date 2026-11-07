"""Integration tests for /api/tonight + the site_* settings.

The endpoint joins three things (settings, the OpenNGC catalog, and
the user's `targets` table), so the tests below mostly pin the contract
of the join (do session counts show up? does an unconfigured site 400?
does an empty catalog filter still 200 cleanly?). Sky-math correctness
is covered separately by `tests/test_sky.py`.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import nodes.basic  # noqa: F401  registers downscale (fixture parity with other api tests)
from server import sky, sky_thumbs
from server.api import app, job_manager, project_manager
from server.cache import ContentCache
from server.catalog import showpieces
from server.catalog.db import default_db_path, open_db
from server.catalog.openngc import enrich


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # ASTROLAB_HOME -> tmp keeps the default DB path on disk under tmp_path
    # (the conftest autouse fixture already does this; we re-do it for
    # belt-and-suspenders so this test file is self-contained).
    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    db_path = default_db_path()
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    # Pin the same default DB path into the in-memory managers so both the
    # API's DBDep (open_db with no path) and the managers' own KV settings
    # writes target a single sqlite file. Without this the settings PATCH
    # writes go to one DB and the targets join reads from another.
    job_manager.reset_for_tests(db_path=db_path)
    project_manager.reset_for_tests(db_path=db_path)
    with TestClient(app) as c:
        yield c


def _set_site(client: TestClient, lat=40.7, lon=-74.0, elev=10.0) -> None:
    r = client.patch(
        "/api/settings",
        json={
            "site_latitude": lat,
            "site_longitude": lon,
            "site_elevation_m": elev,
        },
    )
    assert r.status_code == 200, r.text


def test_settings_round_trip_includes_site(client: TestClient) -> None:
    """The settings GET surfaces site_* keys, defaulting to None when
    nothing's been configured."""
    body = client.get("/api/settings").json()
    assert body["site_latitude"] is None
    assert body["site_longitude"] is None
    assert body["site_elevation_m"] is None

    r = client.patch(
        "/api/settings",
        json={
            "site_latitude": 40.7,
            "site_longitude": -74.0,
            "site_elevation_m": 10.0,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["site_latitude"] == 40.7
    assert body["site_longitude"] == -74.0
    assert body["site_elevation_m"] == 10.0
    # Persisted: a fresh GET reads it back.
    body2 = client.get("/api/settings").json()
    assert body2["site_latitude"] == 40.7


def test_settings_rejects_lat_out_of_range(client: TestClient) -> None:
    r = client.patch("/api/settings", json={"site_latitude": 91.0})
    assert r.status_code == 400


def test_settings_rejects_lon_out_of_range(client: TestClient) -> None:
    r = client.patch("/api/settings", json={"site_longitude": -181.0})
    assert r.status_code == 400


def test_settings_rejects_implausible_elevation(client: TestClient) -> None:
    r = client.patch("/api/settings", json={"site_elevation_m": 30000.0})
    assert r.status_code == 400


def test_tonight_400_when_site_not_configured(client: TestClient) -> None:
    """Without site config we don't fall back to (0,0,0) silently;
    the user sees a clear error and goes to set their location."""
    r = client.get("/api/tonight")
    assert r.status_code == 400
    assert "site location" in r.json()["detail"].lower()


def test_tonight_400_with_partial_site_config(client: TestClient) -> None:
    client.patch("/api/settings", json={"site_latitude": 40.7})
    r = client.get("/api/tonight")
    assert r.status_code == 400


def test_tonight_returns_entries_with_known_site(client: TestClient) -> None:
    """Smoke test: with NYC configured and a fixed October timestamp,
    we expect a non-empty list of visible targets above the default
    20deg / mag-12 thresholds."""
    _set_site(client)
    r = client.get("/api/tonight?at=2024-10-15T04:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["site_latitude"] == 40.7
    assert isinstance(body["entries"], list)
    assert len(body["entries"]) > 5
    scores = [e["score"] for e in body["entries"]]
    assert scores == sorted(scores, reverse=True)
    # Each entry obeys the filter contract.
    for entry in body["entries"]:
        assert entry["hours_above_min_alt"] > 0
        assert entry["magnitude"] is None or entry["magnitude"] <= body["max_magnitude"]
        assert "ra_deg" in entry and "dec_deg" in entry


def test_tonight_includes_m31_in_october_nyc(client: TestClient) -> None:
    """M31 is a high, bright target from NYC in October. OpenNGC stores
    it under the canonical "NGC 224"; we test for either form so a
    future canonical-name change in the catalog stays caught."""
    _set_site(client)
    body = client.get("/api/tonight?at=2024-10-15T04:00:00Z").json()
    names = {e["name"] for e in body["entries"]}
    assert "M 31" in names or "NGC 224" in names


def test_tonight_filter_chips_propagate(client: TestClient) -> None:
    """min_alt and max_mag query params actually filter the result."""
    _set_site(client)
    base = client.get("/api/tonight?at=2024-10-15T04:00:00Z").json()
    strict = client.get(
        "/api/tonight?at=2024-10-15T04:00:00Z&min_alt=70&max_mag=8"
    ).json()
    # Tighter filters yield strictly fewer entries; equal counts would mean
    # the params didn't bite at all.
    assert len(strict["entries"]) < len(base["entries"])
    for entry in strict["entries"]:
        assert entry["hours_above_min_alt"] > 0
        assert entry["magnitude"] is None or entry["magnitude"] <= 8.0


def test_tonight_resolves_garbage_name_via_position(client: TestClient) -> None:
    """A target whose name doesn't resolve but whose resolved_canonical
    is set (by the scanner's position-fallback) lights up the captured-
    only overlay keyed by that canonical id. Pinned because this is the
    headline behavior of the resolve pass."""
    _set_site(client)
    with open_db() as conn, conn:
        conn.execute(
            "INSERT INTO targets (name, resolved_canonical, resolved_source) "
            "VALUES (?, ?, ?)",
            ("MY_GARBAGE_NAME", "NGC 7000", "position"),
        )
        target_id = conn.execute(
            "SELECT id FROM targets WHERE name = ?",
            ("MY_GARBAGE_NAME",),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO sessions (
                scope_id, target_id, started_at,
                frame_count, failed_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("dwarf3", target_id, "2024-10-01T20:00:00Z", 30, 0),
        )
    body = client.get("/api/tonight?at=2024-10-15T04:00:00Z").json()
    ngc7k = next(
        (e for e in body["entries"] if e["name"] == "NGC 7000"),
        None,
    )
    assert ngc7k is not None, [e["name"] for e in body["entries"][:20]]
    assert ngc7k["session_count"] == 1


def test_tonight_session_count_joins_targets_table(client: TestClient) -> None:
    """When the user has captured M 31, the corresponding row's
    session_count > 0. Demonstrates the canonical-name join works
    across the slightly different name conventions OpenNGC ('M 31')
    uses vs whatever the scanner inserted."""
    _set_site(client)
    # Insert a fake target so the join populates session_count.
    with open_db() as conn, conn:
        conn.execute(
            "INSERT INTO targets (name) VALUES (?)", ("M 31",)
        )
        target_id = conn.execute(
            "SELECT id FROM targets WHERE name = ?", ("M 31",)
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO sessions (
                scope_id, target_id, started_at,
                frame_count, failed_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("dwarf3", target_id, "2024-10-01T20:00:00Z", 30, 1),
        )
        conn.execute(
            """
            INSERT INTO sessions (
                scope_id, target_id, started_at,
                frame_count, failed_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("dwarf3", target_id, "2024-10-15T20:00:00Z", 50, 0),
        )

    body = client.get("/api/tonight?at=2024-10-15T04:00:00Z").json()
    # OpenNGC's canonical for M 31 is NGC 224; the join resolves the
    # "M 31" alias the targets table uses against that canonical row.
    m31 = next(
        (e for e in body["entries"] if e["name"] in ("NGC 224", "M 31")),
        None,
    )
    assert m31 is not None, [e["name"] for e in body["entries"][:10]]
    assert m31["session_count"] == 2
    assert m31["last_session_at"] == "2024-10-15T20:00:00Z"


def test_tonight_with_custom_target_name_no_crash(client: TestClient) -> None:
    """A targets-table row whose name doesn't resolve to any OpenNGC
    entry (e.g. an arbitrary user-named region) shouldn't crash the
    join. It just doesn't show up in any catalog row's session_count
    overlay; the catalog rows still render with zero captures."""
    _set_site(client)
    with open_db() as conn, conn:
        conn.execute(
            "INSERT INTO targets (name) VALUES (?)", ("My backyard test",)
        )
        target_id = conn.execute(
            "SELECT id FROM targets WHERE name = ?", ("My backyard test",)
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO sessions (
                scope_id, target_id, started_at,
                frame_count, failed_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("dwarf3", target_id, "2024-10-01T20:00:00Z", 30, 1),
        )
    r = client.get("/api/tonight?at=2024-10-15T04:00:00Z")
    assert r.status_code == 200


def test_tonight_handles_empty_catalog_quietly(client: TestClient) -> None:
    """No captured targets in the user's table is the default state. The
    endpoint should still 200 and just stamp session_count=0 everywhere."""
    _set_site(client)
    body = client.get("/api/tonight?at=2024-10-15T04:00:00Z").json()
    assert all(e["session_count"] == 0 for e in body["entries"])
    assert all(e["last_session_at"] is None for e in body["entries"])


def test_tonight_default_at_is_now(client: TestClient) -> None:
    """Omitting `at` defaults to the server's current UTC time. The
    response echoes the resolved at_utc so callers know what timestamp
    the math was anchored to."""
    _set_site(client)
    r = client.get("/api/tonight")
    assert r.status_code == 200
    body = r.json()
    assert "at_utc" in body
    # Resolved to a parseable ISO 8601 string.
    from datetime import datetime
    datetime.fromisoformat(body["at_utc"])


def test_tonight_400_on_malformed_at(client: TestClient) -> None:
    _set_site(client)
    r = client.get("/api/tonight?at=not-a-date")
    assert r.status_code == 400
    assert "ISO 8601" in r.json()["detail"]


def test_tonight_at_accepts_naive_iso(client: TestClient) -> None:
    """Naive ISO timestamps are valid (treated as UTC). Pinned because
    a strict tz-required parse would silently break the UI when it
    sends `2024-10-15T04:00:00`."""
    _set_site(client)
    r = client.get("/api/tonight?at=2024-10-15T04:00:00")
    assert r.status_code == 200


def test_tonight_polar_summer_no_window(client: TestClient) -> None:
    """North-pole summer: no twilight window, so dusk/dawn are null and
    every entry's hours_above_min_alt is 0. The endpoint should still
    200, we don't want to hide the page on the user."""
    _set_site(client, lat=89.9, lon=0.0, elev=0.0)
    body = client.get("/api/tonight?at=2024-06-21T12:00:00Z").json()
    assert body["dusk_utc"] is None
    assert body["dawn_utc"] is None
    for entry in body["entries"]:
        assert entry["hours_above_min_alt"] == 0.0
        assert entry["transit_utc"] is None


def test_tonight_southern_hemisphere_filters_north_targets(client: TestClient) -> None:
    """Sydney shouldn't see M31 anywhere near 20 deg. The result list
    excludes far-northern targets when configured for a southern site."""
    _set_site(client, lat=-33.9, lon=151.2, elev=58.0)
    body = client.get("/api/tonight?at=2024-10-15T10:00:00Z").json()
    names = {e["name"] for e in body["entries"]}
    assert "M 31" not in names


def test_tonight_returns_in_reasonable_time(client: TestClient) -> None:
    """The endpoint walks the full OpenNGC catalog (~14k entries) per
    request. With the batched alt/az + transit math, a single call
    should land well under a second on a modern dev box; we use a
    generous 5s ceiling so the test isn't flaky on slow CI hardware
    but still catches an O(n) regression to per-target astropy calls."""
    import time

    _set_site(client)
    t0 = time.perf_counter()
    r = client.get("/api/tonight?at=2024-10-15T04:00:00Z")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 5.0, f"tonight took {elapsed:.2f}s"


def test_tonight_response_shape_matches_dto(client: TestClient) -> None:
    """Pin the contract: every documented key is present and types are
    what the UI expects. Drift in the DTO breaks the frontend type-check
    silently otherwise."""
    _set_site(client)
    body = client.get("/api/tonight?at=2024-10-15T04:00:00Z").json()
    expected_top = {
        "at_utc",
        "site_latitude",
        "site_longitude",
        "site_elevation_m",
        "min_alt_deg",
        "max_magnitude",
        "dusk_utc",
        "dawn_utc",
        "alt_curve_step_min",
        "entries",
    }
    assert expected_top <= set(body.keys())
    assert isinstance(body["alt_curve_step_min"], int)
    assert body["alt_curve_step_min"] > 0
    if body["entries"]:
        sample = body["entries"][0]
        expected_entry = {
            "name",
            "common_name",
            "object_type",
            "constellation",
            "ra_deg",
            "dec_deg",
            "magnitude",
            "messier",
            "size_arcmin",
            "score",
            "image_version",
            "alt_now_deg",
            "az_now_deg",
            "transit_utc",
            "hours_above_min_alt",
            "session_count",
            "last_session_at",
            "alt_curve_deg",
        }
        assert expected_entry == set(sample.keys())
        # When the night window is well-defined, the sparkline must be a
        # non-empty list of plausible altitudes and never report sub-
        # horizon by accident: -90 is fine, +90 is fine, NaN/Inf are not.
        if body["dusk_utc"] is not None and body["dawn_utc"] is not None:
            curve = sample["alt_curve_deg"]
            assert isinstance(curve, list) and len(curve) >= 2
            for v in curve:
                assert -90.0 <= v <= 90.0


def test_tonight_list_does_not_depend_on_time_of_day(client: TestClient) -> None:
    """Opening the page at noon lists the coming night, not the daytime sky."""
    _set_site(client)
    noon = client.get("/api/tonight?at=2024-10-15T16:00:00Z").json()
    evening = client.get("/api/tonight?at=2024-10-15T23:00:00Z").json()
    assert noon["dusk_utc"] == evening["dusk_utc"]
    assert {e["name"] for e in noon["entries"]} == {e["name"] for e in evening["entries"]}
    m31 = next(e for e in noon["entries"] if e["messier"] == "M 31")
    assert m31["alt_now_deg"] < 20


def test_tonight_ranks_showpieces_above_obscure_targets(client: TestClient) -> None:
    _set_site(client)
    body = client.get("/api/tonight?at=2024-11-08T03:00:00Z").json()
    top = [e["name"] for e in body["entries"][:12]]
    assert {"NGC 224", "NGC 7000", "IC 1805", "NGC 1499"} <= set(top)
    plain_cluster = next(e for e in body["entries"] if e["messier"] == "M 52")
    assert body["entries"].index(plain_cluster) > 40


def test_short_session_loses_to_a_full_night_of_a_lesser_showpiece() -> None:
    args = (False, True, 60.0, 6.0)
    best_but_setting = sky.target_score("Nebula", 0.0, *args, 0.5, 10.0)
    full_night = sky.target_score("Nebula", 0.3, *args, 10.0, 10.0)
    assert full_night > best_but_setting > 0


def test_star_on_the_curated_list_still_scores() -> None:
    args = (False, False, None, 8.0, 8.0, 10.0)
    assert sky.target_score("Star", None, *args) == 0.0
    assert sky.target_score("Star", 0.5, *args) > 0.0


def test_tonight_uses_curated_names_and_keeps_named_targets_without_magnitude(
    client: TestClient,
) -> None:
    """IC 1805 has no name in OpenNGC, and NGC 2237 has no magnitude."""
    _set_site(client)
    body = client.get("/api/tonight?at=2025-01-15T04:00:00Z").json()
    by_name = {e["name"]: e for e in body["entries"]}
    assert by_name["IC 1805"]["common_name"] == "Heart Nebula"
    assert by_name["NGC 2237"]["magnitude"] is None


def test_showpiece_list_resolves_every_name_once() -> None:
    """A typo or an alias of an earlier row silently drops a target."""
    names = json.loads(showpieces.DATA_PATH.read_text(encoding="utf-8"))["targets"]
    resolved = [enrich(n) for n in names]
    assert [n for n, e in zip(names, resolved, strict=True) if e is None] == []
    canonical = [e.canonical for e in resolved if e is not None]
    assert len(set(canonical)) == len(canonical)


def test_thumb_unknown_target_404(client: TestClient) -> None:
    assert client.get("/api/tonight/thumb?name=NotAThing").status_code == 404


def test_thumb_upstream_failure_is_502_and_not_cacheable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise sky_thumbs.ThumbUnavailable("down")

    monkeypatch.setattr(sky_thumbs, "get_thumb", fail)
    r = client.get("/api/tonight/thumb?name=M 31")
    assert r.status_code == 502
    assert "immutable" not in r.headers.get("cache-control", "")


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 400), (200, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


def test_uploaded_image_replaces_survey_thumb_until_removed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_site(client)
    monkeypatch.setattr(sky_thumbs, "get_thumb", lambda *a, **k: b"survey")
    assert client.get("/api/tonight/thumb?name=M 31").content == b"survey"

    assert client.put("/api/tonight/image?name=M 31", content=_png()).status_code == 200
    custom = client.get("/api/tonight/thumb?name=M31")
    assert Image.open(io.BytesIO(custom.content)).size == (384, 384)
    entries = client.get("/api/tonight?at=2024-10-15T04:00:00Z").json()["entries"]
    version = next(e for e in entries if e["name"] == "NGC 224")["image_version"]
    assert version is not None

    assert client.delete("/api/tonight/image?name=M 31").status_code == 200
    assert client.get("/api/tonight/thumb?name=M 31").content == b"survey"
    entries = client.get("/api/tonight?at=2024-10-15T04:00:00Z").json()["entries"]
    assert next(e for e in entries if e["name"] == "NGC 224")["image_version"] is None


def test_upload_rejects_a_file_that_is_not_an_image(client: TestClient) -> None:
    r = client.put("/api/tonight/image?name=M 31", content=b"not an image")
    assert r.status_code == 400
    assert client.delete("/api/tonight/image?name=M 31").status_code == 200


def test_upload_rejects_an_unknown_target(client: TestClient) -> None:
    assert client.put("/api/tonight/image?name=Nope", content=_png()).status_code == 404
