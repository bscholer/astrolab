"""The NAS as archive: kept results, offloading captures, fetching them back.

server/results.py keeps every version's picture and the key FITS out of cache
eviction; server/archive.py offloads verified captures when the disk runs
short and fetches them back before a job needs them.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from server import archive, results
from server.cache import DONE_MARKER, ContentCache
from server.catalog.db import connect as open_catalog_db
from server.catalog.scanner import ScanStats, _remove_orphans
from server.jobs import JobRecord, JobWorker
from server.local_usage import measure
from server.models import Job, NodeSpec, Template
from server.nas_backup import image_backup_path
from server.storage import ensure_disk_headroom

OLD = time.time() - 30 * 86400


def _entry(cache: ContentCache, h: str, name: str, payload: bytes, port: str = "image") -> None:
    d = cache.reserve(h)
    (d / name).write_bytes(payload)
    cache.commit(h, {port: _ref(d / name)})


def _ref(path: Path):
    from server.models import Ref
    from server.ports import PortType

    t = PortType.IMAGE_PNG if path.suffix == ".png" else PortType.IMAGE_FITS
    return Ref(path=path, type=t, node_hash="ext", port="image")


def _project(conn: sqlite3.Connection, pid: str, name: str, versions: list[dict], **kw) -> None:
    template = {"outputs": {"image": "save.image", "stacked": "combine.image",
                            "stretched": "stretch.image"}}
    with conn:
        conn.execute(
            "INSERT INTO projects (id, name, template_id, template_version, template_json,"
            " base_job_json, current_seq, draft_mode, source_session_ids, created_at,"
            " updated_at, cover_seq) VALUES (?, ?, 't', 1, '{}', '{}', ?, 0, '[]', '', '', ?)",
            (pid, name, kw.get("current", len(versions) - 1), kw.get("cover")),
        )
        for seq, outputs in enumerate(versions):
            jid = f"{pid}-{seq}"
            conn.execute(
                "INSERT INTO jobs (id, status, template_id, template_version, template_json,"
                " job_json, outputs_json, submitted_at) VALUES (?, 'completed', 't', 1, ?, '{}', ?, '')",
                (jid, json.dumps(template), json.dumps(outputs)),
            )
            conn.execute(
                "INSERT INTO project_history (project_id, seq, job_id, overrides_json, created_at,"
                " published, kind) VALUES (?, ?, ?, '{}', '', 0, 'edit')",
                (pid, seq, jid),
            )


def _outputs(seq: int) -> dict:
    return {
        port: {"path": f"/cache/{h}/{f}", "type": t, "node_hash": h, "display_ready": False}
        for port, h, f, t in (
            ("image", f"img{seq}", "image.png", "image/png"),
            ("stacked", f"stk{seq}", "image.fit", "image/fits"),
            ("stretched", f"str{seq}", "image.fit", "image/fits"),
        )
    }


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = open_catalog_db(tmp_path / "catalog.sqlite")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_kept_results_survive_eviction_and_come_back_on_lookup(tmp_path, db) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    for seq in range(3):
        _entry(cache, f"img{seq}", "image.png", b"png")
        _entry(cache, f"stk{seq}", "image.fit", b"stack")
        _entry(cache, f"str{seq}", "image.fit", b"final")
    _project(db, "p1", "Veil", [_outputs(s) for s in range(3)])

    stats = results.reconcile(cache, db)

    # Every picture; the FITS only for the first and the latest version.
    kept = {p.name for p in cache.kept_root.iterdir()}
    assert kept == {"img0", "img1", "img2", "stk0", "str0", "stk2", "str2"}
    assert stats["linked"] == 7
    # Links, not copies.
    assert os.stat(cache.kept_dir("img1") / "image.png").st_ino == os.stat(
        cache.entry_dir("img1") / "image.png"
    ).st_ino

    shutil.rmtree(cache.entry_dir("img1"))  # evicted
    assert cache.lookup("img1") is not None
    assert (cache.entry_dir("img1") / "image.png").read_bytes() == b"png"
    assert cache.load_outputs("img1")["image"].path.name == "image.png"


def test_the_cover_keeps_its_fits_and_deleted_projects_go(tmp_path, db) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    for seq in range(3):
        _entry(cache, f"img{seq}", "image.png", b"png")
        _entry(cache, f"stk{seq}", "image.fit", b"stack")
        _entry(cache, f"str{seq}", "image.fit", b"final")
    _project(db, "p1", "Veil", [_outputs(s) for s in range(3)], cover=1)
    results.reconcile(cache, db)
    assert (cache.kept_dir("stk1") / DONE_MARKER).exists()

    with db:
        db.execute("DELETE FROM project_history")
        db.execute("DELETE FROM projects")
    stats = results.reconcile(cache, db)
    assert stats["dropped"] == 9
    assert list(cache.kept_root.iterdir()) == []


def test_a_result_gone_locally_comes_back_from_the_nas_backup(tmp_path, db) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    images = tmp_path / "nas" / "images"
    backup = image_backup_path(images, "Veil", 0, "image", ".png")
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"from the nas")
    _project(db, "p1", "Veil", [{"image": _outputs(0)["image"]}])

    stats = results.reconcile(cache, db, images)

    assert stats["from_nas"] == 1
    assert cache.lookup("img0") is not None
    ref = cache.load_outputs("img0")["image"]
    assert ref.path.read_bytes() == b"from the nas"


def _session_folder(captures: Path, name: str, mtime: float = OLD) -> Path:
    folder = captures / f"DWARF_RAW_TELE_{name}"
    folder.mkdir(parents=True)
    for n in ("light_0001.fits", "light_0002.fits", "failed_0003.fits"):
        (folder / n).write_bytes(b"x" * 1000)
        os.utime(folder / n, (mtime, mtime))
    return folder


def _backup(folder: Path, nas: Path) -> None:
    from server.paths import nas_captures

    dest = nas_captures(nas) / folder.name
    dest.mkdir(parents=True, exist_ok=True)
    for f in folder.iterdir():
        if not f.name.startswith("failed_"):
            shutil.copy2(f, dest / f.name)


def test_offload_takes_only_settled_kept_folders_fully_on_the_nas(
    tmp_path, db, monkeypatch
) -> None:
    captures, nas = tmp_path / "captures", tmp_path / "nas"
    old = _session_folder(captures, "M_33")
    recent = _session_folder(captures, "M_81", mtime=time.time())
    partial = _session_folder(captures, "M_45")
    for f in (old, recent):
        _backup(f, nas)
    _backup(partial, nas)
    (nas / "captures" / partial.name / "light_0002.fits").write_bytes(b"short")
    monkeypatch.setattr(archive, "kept_folders", lambda _c: {str(old), str(recent), str(partial)})

    freed = archive.offload(db, captures, nas, need_bytes=10**9)

    assert freed == 3000  # the whole folder, failed frame included
    assert not old.exists() and recent.exists() and partial.exists()
    row = archive.archived(db)[str(old)]
    assert row.bytes == 2000 and row.files == 2 and not row.local
    assert archive.archived_names(db) == {old.name}


def test_a_job_fetches_offloaded_frames_back_first(tmp_path, db, monkeypatch) -> None:
    captures, nas = tmp_path / "captures", tmp_path / "nas"
    folder = _session_folder(captures, "M_33")
    _backup(folder, nas)
    stage = tmp_path / "lights_stage" / "abc"
    stage.mkdir(parents=True)
    for f in ("light_0001.fits", "light_0002.fits"):
        (stage / f"s1__{f}").symlink_to(folder / f)
    monkeypatch.setattr(archive, "kept_folders", lambda _c: {str(folder)})
    archive.offload(db, captures, nas, need_bytes=1)
    inputs = {"convert@1.lights": {"path": str(stage), "type": "sequence/fits"}}
    assert [a.folder for a in archive.needed(db, inputs)] == [str(folder)]

    seen: list[int] = []
    archive.fetch(db, archive.needed(db, inputs)[0], on_bytes=seen.append)

    assert (folder / "light_0001.fits").read_bytes() == b"x" * 1000
    assert all(link.resolve().exists() for link in stage.iterdir())
    assert sum(seen) == 2000
    assert archive.needed(db, inputs) == []
    assert archive.archived(db)[str(folder)].local  # rows of its failed frame stay
    # Local again, so it can go again.
    assert archive.offload(db, captures, nas, need_bytes=1) > 0


def test_the_scanner_keeps_rows_of_offloaded_frames(tmp_path, db) -> None:
    captures = tmp_path / "captures"
    folder = captures / "DWARF_RAW_TELE_M_33"
    with db:
        for name in ("light_0001.fits", "failed_0002.fits"):
            db.execute("INSERT INTO frames (path, scanned_at) VALUES (?, 0)", (str(folder / name),))
        db.execute("INSERT INTO frames (path, scanned_at) VALUES (?, 0)",
                   (str(captures / "DWARF_RAW_TELE_M_81" / "light_0001.fits"),))
        db.execute(
            "INSERT INTO archived_folders (folder, nas_path, bytes, files, archived_at)"
            " VALUES (?, '/nas/x', 1, 1, 0)", (str(folder),),
        )
    captures.mkdir()
    _remove_orphans(db, captures, time.time(), ScanStats())
    left = sorted(Path(r[0]).name for r in db.execute("SELECT path FROM frames"))
    assert left == ["failed_0002.fits", "light_0001.fits"]


def test_headroom_offloads_captures_only_after_the_cache_sweep(tmp_path, monkeypatch) -> None:
    from unittest.mock import patch

    monkeypatch.setenv("ASTROLAB_DISK_FLOOR_FRACTION", "0")
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    disks = iter([10_000, 10_000, 50_000])
    disk = lambda _p: type("D", (), {"total_bytes": 10**6, "used_bytes": 0,  # noqa: E731
                                     "free_bytes": next(disks)})
    asked: list[int] = []

    def offload(n: int) -> int:
        asked.append(n)
        return n

    with patch("server.storage._disk_usage", side_effect=disk):
        ensure_disk_headroom(
            cache, need_bytes=30_000, cache_max_bytes=10**9, db_path=db_path, offload=offload
        )
    assert asked == [20_000]


def test_a_job_whose_frames_are_on_an_unreachable_nas_fails_plainly(
    tmp_path, monkeypatch
) -> None:
    from server import jobs as jobs_mod

    db_path = tmp_path / "catalog.sqlite"
    conn = open_catalog_db(db_path)
    stage = tmp_path / "lights_stage" / "abc"
    stage.mkdir(parents=True)
    (stage / "s1__light.fits").symlink_to(tmp_path / "captures" / "F" / "light.fits")
    with conn:
        conn.execute(
            "INSERT INTO archived_folders (folder, nas_path, bytes, files, archived_at)"
            " VALUES (?, '/nowhere', 10, 1, 0)", (str(tmp_path / "captures" / "F"),),
        )
    template = Template(id="t", version=1, description="",
                        nodes=[NodeSpec(id="n", kind="downscale", params={})],
                        outputs={"image": "n.image"})
    job = Job(template_id="t", template_version=1, inputs={
        "convert@1.lights": {"path": str(stage), "type": "sequence/fits", "node_hash": "ext", "port": "lights"}})
    record = JobRecord(id="j", status="running", template=template, job=job,
                       submitted_at="2026-01-01T00:00:00Z")
    with conn:
        conn.execute(
            "INSERT INTO jobs (id, status, template_id, template_version, template_json,"
            " job_json, submitted_at) VALUES ('j', 'running', 't', 1, ?, '{}', '')",
            (json.dumps(template.model_dump(mode="json")),),
        )
    conn.close()
    ran = threading.Event()
    monkeypatch.setattr(jobs_mod, "run_job", lambda *a, **k: ran.set() or {})
    worker = JobWorker(cache=ContentCache(root=tmp_path / "cache"), db_path=db_path)
    worker._run(record)

    conn = sqlite3.connect(db_path)
    status, error = conn.execute("SELECT status, error FROM jobs WHERE id = 'j'").fetchone()
    assert (status, ran.is_set()) == ("failed", False)
    assert "on the NAS" in error


def test_local_usage_counts_a_kept_link_once_as_a_result(tmp_path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    _entry(cache, "img0", "image.png", b"p" * 4000)
    _entry(cache, "stk0", "image.fit", b"s" * 9000)
    cache.kept_root.mkdir()
    from server.cache import link_tree

    link_tree(cache.entry_dir("img0"), cache.kept_dir("img0"))
    captures = tmp_path / "captures"
    (captures / "DWARF_RAW_X").mkdir(parents=True)
    (captures / "DWARF_RAW_X" / "light.fits").write_bytes(b"l" * 2000)

    got = {k["id"]: k["bytes"] for k in measure(cache.root, captures)["kinds"]}

    assert got["results"] >= 4000 and got["results"] < 4000 + 1000  # + manifest files
    assert 9000 <= got["cache"] < 9000 + 1000
    assert got["captures"] == 2000

