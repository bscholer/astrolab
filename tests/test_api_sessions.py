"""Integration tests for the per-session reassign endpoints.

The Library page used to surface a target-level override editor; that's
gone. In its place a session row carries a pencil that opens the
reassign popup, hitting these endpoints:

- PATCH /api/sessions/{id}                   reassigns a session
- GET   /api/sessions/{id}/reassign_candidates  suggests targets + catalog rows
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.catalog.adapters  # noqa: F401  registers dwarf3
from server.api import app, job_manager, project_manager
from server.cache import ContentCache
from server.catalog.db import default_db_path, open_db
from server.catalog.scanner import scan as run_scan

from ._fits_fixtures import DEFAULT_LIGHT_HEADER, write_fits

# OpenNGC positions for the two targets we use across the tests.
NGC7000_RA = 314.75
NGC7000_DEC = 44.53
M31_RA = 10.68
M31_DEC = 41.27


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROLAB_HOME", str(tmp_path))
    db_path = default_db_path()
    monkeypatch.setattr(job_manager, "_cache", ContentCache(root=tmp_path / "cache"))
    job_manager.reset_for_tests(db_path=db_path)
    project_manager.reset_for_tests(db_path=db_path)
    with TestClient(app) as c:
        yield c


def _build_session(
    root: Path,
    *,
    object_name: str,
    ra: float | None,
    dec: float | None,
    n_frames: int = 4,
    folder_safe_name: str | None = None,
) -> None:
    folder_name = folder_safe_name or object_name
    folder = (
        root
        / f"DWARF_RAW_TELE_{folder_name}_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    )
    base_hdr = dict(DEFAULT_LIGHT_HEADER)
    base_hdr["OBJECT"] = object_name
    if ra is not None:
        base_hdr["RA"] = ra
    else:
        base_hdr.pop("RA", None)
    if dec is not None:
        base_hdr["DEC"] = dec
    else:
        base_hdr.pop("DEC", None)
    for i in range(n_frames):
        write_fits(
            folder / f"{folder_name}_30s60_Astro_2025102{i % 10}-22192950{i}_24C.fits",
            headers=base_hdr,
        )


def _ids(client: TestClient) -> tuple[int, int]:
    """Return (session_id, target_id) for the first session in the catalog."""
    body = client.get("/api/targets").json()
    target_id = body[0]["id"]
    detail = client.get(f"/api/targets/{target_id}").json()
    return detail["sessions"][0]["id"], target_id


def test_patch_target_id_reassigns_session(
    client: TestClient, tmp_path: Path
) -> None:
    """Reassigning by target_id moves sessions.target_id without touching frames."""
    captures = tmp_path / "caps"
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC)
    _build_session(
        captures,
        object_name="MY_GARBAGE",
        ra=NGC7000_RA,
        dec=NGC7000_DEC,
        folder_safe_name="MY_GARBAGE",
    )
    run_scan(captures)

    # Pick the M 31 session, move it to MY_GARBAGE.
    with open_db() as conn:
        m31 = conn.execute(
            "SELECT s.id AS id, s.target_id AS target_id FROM sessions s "
            "JOIN targets t ON t.id = s.target_id WHERE t.name = ?",
            ("M 31",),
        ).fetchone()
        garbage_target_id = conn.execute(
            "SELECT id FROM targets WHERE name = ?", ("MY_GARBAGE",)
        ).fetchone()["id"]
    session_id = int(m31["id"])
    r = client.patch(
        f"/api/sessions/{session_id}",
        json={"target_id": int(garbage_target_id)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session"]["target_name"] == "MY_GARBAGE"

    with open_db() as conn:
        moved = conn.execute(
            "SELECT target_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        # Frames are still bound by session_key, unchanged.
        frame_session_key = conn.execute(
            "SELECT session_key FROM frames WHERE session_key IS NOT NULL "
            "LIMIT 1"
        ).fetchone()
    assert int(moved["target_id"]) == int(garbage_target_id)
    assert frame_session_key is not None


def test_patch_new_target_name_creates_and_moves(
    client: TestClient, tmp_path: Path
) -> None:
    """new_target_name without an existing match creates a fresh row."""
    captures = tmp_path / "caps"
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC)
    run_scan(captures)
    session_id, _ = _ids(client)

    r = client.patch(
        f"/api/sessions/{session_id}",
        json={"new_target_name": "NGC 7000"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session"]["target_name"] == "NGC 7000"
    # M 31's row had only this one session -> it should be deleted.
    assert len(body["deleted_target_ids"]) == 1

    with open_db() as conn:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM targets").fetchall()
        }
    assert "NGC 7000" in names
    assert "M 31" not in names


def test_patch_empties_source_target_deletes_it(
    client: TestClient, tmp_path: Path
) -> None:
    """If the source target has no remaining sessions, it gets dropped."""
    captures = tmp_path / "caps"
    _build_session(captures, object_name="LONE", ra=M31_RA, dec=M31_DEC)
    _build_session(
        captures,
        object_name="OTHER",
        ra=NGC7000_RA,
        dec=NGC7000_DEC,
        folder_safe_name="OTHER",
    )
    run_scan(captures)

    with open_db() as conn:
        lone = conn.execute(
            "SELECT s.id AS id, t.id AS tid FROM sessions s "
            "JOIN targets t ON t.id = s.target_id WHERE t.name = ?",
            ("LONE",),
        ).fetchone()
        other_tid = conn.execute(
            "SELECT id FROM targets WHERE name = ?", ("OTHER",)
        ).fetchone()["id"]

    r = client.patch(
        f"/api/sessions/{int(lone['id'])}",
        json={"target_id": int(other_tid)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_target_ids"] == [int(lone["tid"])]
    with open_db() as conn:
        gone = conn.execute(
            "SELECT id FROM targets WHERE id = ?", (int(lone["tid"]),)
        ).fetchone()
    assert gone is None


def test_patch_new_target_name_reuses_existing(
    client: TestClient, tmp_path: Path
) -> None:
    """A new_target_name that matches an existing (normalized) target
    reuses it rather than duplicating. The user said "reassigning should
    straight up change it", so collisions merge, not raise."""
    captures = tmp_path / "caps"
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC)
    _build_session(
        captures,
        object_name="MY_GARBAGE",
        ra=NGC7000_RA,
        dec=NGC7000_DEC,
        folder_safe_name="MY_GARBAGE",
    )
    run_scan(captures)

    with open_db() as conn:
        garbage = conn.execute(
            "SELECT s.id AS id FROM sessions s "
            "JOIN targets t ON t.id = s.target_id WHERE t.name = ?",
            ("MY_GARBAGE",),
        ).fetchone()
        m31_id = conn.execute(
            "SELECT id FROM targets WHERE name = ?", ("M 31",)
        ).fetchone()["id"]

    # Whitespace + odd casing still normalize-collides into "M 31".
    r = client.patch(
        f"/api/sessions/{int(garbage['id'])}",
        json={"new_target_name": "  M 31  "},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session"]["target_name"] == "M 31"

    # And no duplicate row was inserted: just the existing M 31 target.
    with open_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM targets WHERE name = ?", ("M 31",)
        ).fetchone()["n"]
    assert count == 1
    # The reassigned session is now under the existing target id.
    with open_db() as conn:
        tid = conn.execute(
            "SELECT target_id FROM sessions WHERE id = ?", (int(garbage["id"]),)
        ).fetchone()["target_id"]
    assert int(tid) == int(m31_id)


def test_patch_404_on_unknown_session(client: TestClient) -> None:
    r = client.patch(
        "/api/sessions/99999", json={"target_id": 1}
    )
    assert r.status_code == 404


def test_patch_404_on_unknown_target_id(
    client: TestClient, tmp_path: Path
) -> None:
    captures = tmp_path / "caps"
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC)
    run_scan(captures)
    session_id, _ = _ids(client)
    r = client.patch(
        f"/api/sessions/{session_id}", json={"target_id": 99999}
    )
    assert r.status_code == 404


def test_patch_rejects_both_keys(client: TestClient, tmp_path: Path) -> None:
    """Both target_id AND new_target_name in the body is a 4xx (ambiguous)."""
    captures = tmp_path / "caps"
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC)
    run_scan(captures)
    session_id, _ = _ids(client)
    r = client.patch(
        f"/api/sessions/{session_id}",
        json={"target_id": 1, "new_target_name": "NGC 7000"},
    )
    assert r.status_code in (400, 422)


def test_patch_with_neither_key_is_noop(client: TestClient, tmp_path: Path) -> None:
    """An empty body is a no-op now that PATCH also carries description.
    Returns 200 with the unchanged session; no target deleted."""
    captures = tmp_path / "caps"
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC)
    run_scan(captures)
    session_id, _ = _ids(client)
    r = client.patch(f"/api/sessions/{session_id}", json={})
    assert r.status_code == 200
    assert r.json()["deleted_target_ids"] == []


def test_get_candidates_shape(client: TestClient, tmp_path: Path) -> None:
    """The candidates endpoint returns parallel `targets` and `catalog` lists,
    each sorted by ascending separation."""
    captures = tmp_path / "caps"
    _build_session(
        captures, object_name="NEAR_7000", ra=NGC7000_RA, dec=NGC7000_DEC
    )
    run_scan(captures)
    session_id, _ = _ids(client)
    r = client.get(f"/api/sessions/{session_id}/reassign_candidates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "targets" in body and "catalog" in body
    # Catalog has at least NGC 7000 (the obvious neighbor of these coordinates).
    canonicals = {c["canonical"] for c in body["catalog"]}
    assert "NGC 7000" in canonicals
    seps = [c["separation_arcmin"] for c in body["catalog"]]
    assert seps == sorted(seps)


def test_get_candidates_empty_when_no_centroid(
    client: TestClient, tmp_path: Path
) -> None:
    """A session whose frames lack RA/Dec returns empty lists; the UI
    falls back to a free-text target picker."""
    captures = tmp_path / "caps"
    _build_session(captures, object_name="NO_POS", ra=None, dec=None)
    run_scan(captures)
    session_id, _ = _ids(client)
    r = client.get(f"/api/sessions/{session_id}/reassign_candidates")
    assert r.status_code == 200
    body = r.json()
    assert body["targets"] == []
    assert body["catalog"] == []


def test_get_candidates_excludes_current_target(
    client: TestClient, tmp_path: Path
) -> None:
    """The session's current target shouldn't show up as a reassign option."""
    captures = tmp_path / "caps"
    # Two targets sitting in the same neighborhood: M 31's position vs M 32
    # are close enough that they may suggest each other. Easier: re-use the
    # same RA/Dec for two named targets so both pop up as candidates.
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC)
    _build_session(
        captures,
        object_name="M 32",
        ra=M31_RA,
        dec=M31_DEC,
        folder_safe_name="M 32",
    )
    run_scan(captures)
    with open_db() as conn:
        m31_session = conn.execute(
            "SELECT s.id AS id FROM sessions s "
            "JOIN targets t ON t.id = s.target_id WHERE t.name = ?",
            ("M 31",),
        ).fetchone()
    r = client.get(
        f"/api/sessions/{int(m31_session['id'])}/reassign_candidates"
    )
    body = r.json()
    target_names = {c["name"] for c in body["targets"]}
    assert "M 31" not in target_names
