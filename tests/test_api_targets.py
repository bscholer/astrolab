"""Integration tests for /api/targets/{id} PATCH and /api/targets/{id}/nearby.

The PATCH route lets the user pin a canonical id as the target's resolution.
The /nearby route powers the override dropdown by returning sorted candidate
catalog rows within the suggestion tolerance.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.api import app, job_manager, project_manager
from server.cache import ContentCache
from server.catalog.db import default_db_path, open_db


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    db_path = default_db_path()
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    job_manager.reset_for_tests(db_path=db_path)
    project_manager.reset_for_tests(db_path=db_path)
    with TestClient(app) as c:
        yield c


def _insert_target(name: str, **cols) -> int:
    """Insert a `targets` row directly; returns the new id. Lets us
    construct exact target states without running the scanner."""
    with open_db() as conn, conn:
        col_names = ["name"] + list(cols.keys())
        placeholders = ",".join("?" for _ in col_names)
        conn.execute(
            f"INSERT INTO targets ({','.join(col_names)}) VALUES ({placeholders})",
            (name, *cols.values()),
        )
        row = conn.execute(
            "SELECT id FROM targets WHERE name = ?", (name,)
        ).fetchone()
        return int(row["id"])


def test_patch_sets_canonical_override(client: TestClient) -> None:
    """Pinning an OpenNGC-resolvable canonical id stores it and
    surfaces it in the response with source='override'."""
    tid = _insert_target("MY_TARGET")
    r = client.patch(
        f"/api/targets/{tid}",
        json={"resolved_canonical_override": "NGC 7000"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved_as"] is not None
    assert body["resolved_as"]["canonical"] == "NGC 7000"
    assert body["resolved_as"]["source"] == "override"


def test_patch_canonicalizes_alias(client: TestClient) -> None:
    """PATCH "M 31" stores the canonical form "NGC 224", so the Tonight
    join key is the same regardless of which alias the user typed.
    Pinned: we deliberately don't store the raw alias because the
    captured-overlay merges on canonical, not user input."""
    tid = _insert_target("MY_OTHER_TARGET")
    r = client.patch(
        f"/api/targets/{tid}",
        json={"resolved_canonical_override": "M 31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved_as"]["canonical"] == "NGC 224"
    # And the persisted column matches the canonical form too.
    with open_db() as conn:
        row = conn.execute(
            "SELECT resolved_canonical_override FROM targets WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["resolved_canonical_override"] == "NGC 224"


def test_patch_null_clears_override(client: TestClient) -> None:
    """Passing null clears the override; resolved_as falls back to the
    auto-resolved state."""
    tid = _insert_target(
        "GARBAGE",
        resolved_canonical="NGC 7000",
        resolved_separation_arcmin=10.0,
        resolved_source="position",
        resolved_canonical_override="NGC 224",
    )
    r = client.patch(
        f"/api/targets/{tid}",
        json={"resolved_canonical_override": None},
    )
    assert r.status_code == 200
    body = r.json()
    # The auto resolution wins again.
    assert body["resolved_as"]["canonical"] == "NGC 7000"
    assert body["resolved_as"]["source"] == "position"


def test_patch_null_returns_none_when_no_auto_match(client: TestClient) -> None:
    """Clearing the override when nothing was auto-resolved leaves
    resolved_as as None (the library reads quiet)."""
    tid = _insert_target(
        "GARBAGE_NO_AUTO",
        resolved_canonical_override="NGC 7000",
    )
    r = client.patch(
        f"/api/targets/{tid}",
        json={"resolved_canonical_override": None},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved_as"] is None


def test_patch_rejects_empty_string(client: TestClient) -> None:
    """Whitespace-only strings are a 400; almost certainly a typo, not
    an intentional clear."""
    tid = _insert_target("MY_T")
    r = client.patch(
        f"/api/targets/{tid}",
        json={"resolved_canonical_override": "   "},
    )
    assert r.status_code == 400


def test_patch_rejects_unresolvable_id(client: TestClient) -> None:
    """A value that doesn't resolve in OpenNGC is a 400 with a clear
    detail message; we'd rather surface the typo than store dead state."""
    tid = _insert_target("MY_T")
    r = client.patch(
        f"/api/targets/{tid}",
        json={"resolved_canonical_override": "definitely-not-a-catalog-id"},
    )
    assert r.status_code == 400
    assert "did not resolve" in r.json()["detail"].lower()


def test_patch_404_on_unknown_target(client: TestClient) -> None:
    r = client.patch(
        "/api/targets/99999",
        json={"resolved_canonical_override": "NGC 7000"},
    )
    assert r.status_code == 404


def test_target_list_omits_resolved_as_for_name_source(client: TestClient) -> None:
    """A target with resolved_source='name' deliberately surfaces
    resolved_as=None: the user's stored name already conveys the
    catalog mapping, and the library should read quiet for the obvious
    case."""
    _insert_target(
        "M 31",
        resolved_canonical="NGC 224",
        resolved_separation_arcmin=0.0,
        resolved_source="name",
    )
    r = client.get("/api/targets")
    body = r.json()
    m31_row = next((t for t in body if t["name"] == "M 31"), None)
    assert m31_row is not None
    assert m31_row["resolved_as"] is None


def test_target_list_surfaces_resolved_as_for_position_source(
    client: TestClient,
) -> None:
    """A position-resolved target carries a populated resolved_as
    payload with source='position' and a non-null separation_arcmin."""
    _insert_target(
        "MY_GARBAGE",
        resolved_canonical="NGC 7000",
        resolved_separation_arcmin=22.9,
        resolved_source="position",
    )
    r = client.get("/api/targets")
    body = r.json()
    row = next((t for t in body if t["name"] == "MY_GARBAGE"), None)
    assert row is not None
    assert row["resolved_as"] is not None
    assert row["resolved_as"]["canonical"] == "NGC 7000"
    assert row["resolved_as"]["source"] == "position"
    assert row["resolved_as"]["separation_arcmin"] == 22.9


def test_target_list_canonical_group_for_override(client: TestClient) -> None:
    """A pinned override sets `canonical_group` to that catalog id, and
    `canonical_group_name` to its OpenNGC common name when known."""
    _insert_target("GARBAGE_PINNED", resolved_canonical_override="NGC 7000")
    r = client.get("/api/targets")
    body = r.json()
    row = next((t for t in body if t["name"] == "GARBAGE_PINNED"), None)
    assert row is not None
    assert row["canonical_group"] == "NGC 7000"
    # NGC 7000 has a friendly name in OpenNGC ("North America Nebula").
    assert row["canonical_group_name"] is not None


def test_target_list_canonical_group_for_position(client: TestClient) -> None:
    """A position-resolved target groups under its `resolved_canonical`."""
    _insert_target(
        "GARBAGE_POS",
        resolved_canonical="NGC 7000",
        resolved_separation_arcmin=22.9,
        resolved_source="position",
    )
    r = client.get("/api/targets")
    body = r.json()
    row = next((t for t in body if t["name"] == "GARBAGE_POS"), None)
    assert row is not None
    assert row["canonical_group"] == "NGC 7000"


def test_target_list_canonical_group_for_name_source(client: TestClient) -> None:
    """A name-resolved target groups under its `resolved_canonical` even
    though `resolved_as` itself is suppressed for the name case."""
    _insert_target(
        "M 31",
        resolved_canonical="NGC 224",
        resolved_separation_arcmin=0.0,
        resolved_source="name",
    )
    r = client.get("/api/targets")
    body = r.json()
    row = next((t for t in body if t["name"] == "M 31"), None)
    assert row is not None
    # The caption is suppressed for the name case, but the bucket still
    # has to land on the canonical so two "M 31"-style rows merge cleanly.
    assert row["resolved_as"] is None
    assert row["canonical_group"] == "NGC 224"


def test_target_list_canonical_group_falls_back_to_name(
    client: TestClient,
) -> None:
    """A target with no persisted canonical but whose stored name resolves
    in OpenNGC still gets a `canonical_group` (re-enriched on the fly).
    Without this, freshly-inserted name-resolvable targets would land in
    the unresolved bucket until the scanner re-ran."""
    _insert_target("M 31")
    r = client.get("/api/targets")
    body = r.json()
    row = next((t for t in body if t["name"] == "M 31"), None)
    assert row is not None
    assert row["canonical_group"] == "NGC 224"


def test_target_list_canonical_group_none_for_unresolved(
    client: TestClient,
) -> None:
    """A target with no resolution at all gets a null `canonical_group`;
    the UI buckets these under the Unresolved section."""
    _insert_target("MY_UNKNOWN_TARGET")
    r = client.get("/api/targets")
    body = r.json()
    row = next((t for t in body if t["name"] == "MY_UNKNOWN_TARGET"), None)
    assert row is not None
    assert row["canonical_group"] is None
    assert row["canonical_group_name"] is None


def test_target_list_two_targets_share_group(client: TestClient) -> None:
    """The whole point of the slice: pinning one target's override to a
    canonical that another target already resolved to lands them in the
    same `canonical_group`. The display layer then merges the two rows."""
    _insert_target(
        "C 20",
        resolved_canonical="NGC 7000",
        resolved_separation_arcmin=12.0,
        resolved_source="position",
    )
    _insert_target("GARBAGE_NEAR", resolved_canonical_override="NGC 7000")
    r = client.get("/api/targets")
    body = r.json()
    c20 = next((t for t in body if t["name"] == "C 20"), None)
    other = next((t for t in body if t["name"] == "GARBAGE_NEAR"), None)
    assert c20 is not None and other is not None
    assert c20["canonical_group"] == "NGC 7000"
    assert other["canonical_group"] == "NGC 7000"
    # And the common-name label is identical so the UI's group header is
    # stable regardless of which row "won" the lookup.
    assert c20["canonical_group_name"] == other["canonical_group_name"]


def test_target_list_override_differs_from_auto(client: TestClient) -> None:
    """When a target carries BOTH an auto-resolve and an override, the
    override wins for `canonical_group` (the user's pin is the source of
    truth)."""
    _insert_target(
        "DUAL",
        resolved_canonical="NGC 7000",
        resolved_separation_arcmin=2.0,
        resolved_source="position",
        resolved_canonical_override="NGC 224",
    )
    r = client.get("/api/targets")
    body = r.json()
    row = next((t for t in body if t["name"] == "DUAL"), None)
    assert row is not None
    assert row["canonical_group"] == "NGC 224"


def test_get_nearby_empty_when_no_centroid(client: TestClient) -> None:
    """A target with no frames (and therefore no centroid) returns an
    empty list rather than a 400; the UI can still render the dropdown
    with just the (auto-resolved) / Other... options."""
    tid = _insert_target("NO_FRAMES")
    r = client.get(f"/api/targets/{tid}/nearby")
    assert r.status_code == 200
    assert r.json() == []


def _scan_garbage_at(captures_root: Path, name: str, ra: float, dec: float) -> int:
    """Build a small synthetic capture under captures_root and run the
    scanner. Returns the new target's id."""
    from server.catalog.scanner import scan as run_scan

    from ._fits_fixtures import DEFAULT_LIGHT_HEADER, write_fits
    folder = (
        captures_root
        / f"DWARF_RAW_TELE_{name}_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    )
    hdr = dict(DEFAULT_LIGHT_HEADER)
    hdr["OBJECT"] = name
    hdr["RA"] = ra
    hdr["DEC"] = dec
    for i in range(4):
        write_fits(
            folder / f"{name}_30s60_Astro_2025102{i % 10}-22192950{i}_24C.fits",
            headers=hdr,
        )
    run_scan(captures_root, scope_id="dwarf3")
    with open_db() as conn:
        row = conn.execute(
            "SELECT id FROM targets WHERE name = ?", (name,)
        ).fetchone()
        return int(row["id"])


def test_get_nearby_returns_sorted_list(
    client: TestClient, tmp_path: Path
) -> None:
    """A target near NGC 7000 returns a nearby list with NGC 7000 near
    the top, sorted by ascending separation_arcmin."""
    captures = tmp_path / "caps"
    tid = _scan_garbage_at(captures, "NEAR7000", 314.75, 44.53)
    r = client.get(f"/api/targets/{tid}/nearby")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) > 0
    seps = [e["separation_arcmin"] for e in body]
    assert seps == sorted(seps), seps
    canonicals = {e["canonical"] for e in body}
    assert "NGC 7000" in canonicals


def test_get_nearby_respects_max_sep_deg(
    client: TestClient, tmp_path: Path
) -> None:
    """The max_sep_deg query param widens (or narrows) the catalog scan
    relative to the default tolerance."""
    captures = tmp_path / "caps"
    tid = _scan_garbage_at(captures, "NEAR7000_LIMITS", 314.75, 44.53)
    narrow = client.get(f"/api/targets/{tid}/nearby?max_sep_deg=0.1").json()
    wide = client.get(f"/api/targets/{tid}/nearby?max_sep_deg=5.0").json()
    assert len(wide) >= len(narrow)
    for entry in narrow:
        assert entry["separation_arcmin"] / 60.0 <= 0.1 + 1e-9
