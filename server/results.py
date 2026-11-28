"""Kept results: every project's pictures, and the FITS worth re-editing from.

The cache evicts what it can rebuild, and a session swap purges a project's
entries, so most old versions had lost their picture. Kept results are hard
links to the cache entries that matter, under `<cache root>/_kept/`
(server/cache.py), outside eviction and the cache budget:

- the picture (`image`) of every completed version;
- for a project's first and latest versions, and its cover, the stacked FITS
  and the final FITS (the first of `recombined`, `stretched`). Every finishing
  edit starts from the stack, so re-editing never needs the raw frames.

A link costs no space while the cache entry still exists. When the cache
lookup misses but a kept copy exists, the cache links it back, so previews,
downloads and the runtime see an ordinary hit. With a NAS, a result that is
gone locally but was backed up (server/nas_backup.py) is copied back from
there. Results of deleted projects are dropped.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from server.cache import DONE_MARKER, OUTPUTS_MANIFEST, ContentCache, link_tree
from server.nas_backup import FITS_PORTS, image_backup_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Wanted:
    project: str
    seq: int
    port: str
    """The template's output name (image, stacked, recombined, ...)."""
    node_port: str
    """The producing node's port, as its cache manifest names it."""
    path: str
    type: str
    display_ready: bool


def wanted(conn: sqlite3.Connection) -> dict[str, Wanted]:
    """The cache entries to keep, by node hash."""
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.current_seq, p.cover_seq, h.seq,
               j.outputs_json, j.template_json
        FROM project_history h
        JOIN projects p ON p.id = h.project_id
        JOIN jobs j ON j.id = h.job_id
        WHERE j.status = 'completed' AND j.outputs_json IS NOT NULL
        ORDER BY p.id, h.seq
        """
    ).fetchall()
    versions: dict[str, list[tuple[int, dict, dict]]] = {}
    meta: dict[str, tuple[str, int | None, int | None]] = {}
    for pid, name, current, cover, seq, outputs_json, template_json in rows:
        try:
            outputs = json.loads(outputs_json)
            template = json.loads(template_json) if template_json else {}
        except ValueError:
            continue
        if not isinstance(outputs, dict):
            continue
        mapping = template.get("outputs") if isinstance(template, dict) else None
        versions.setdefault(pid, []).append((seq, outputs, mapping or {}))
        meta[pid] = (name, current, cover)

    out: dict[str, Wanted] = {}
    for pid, vs in versions.items():
        name, current, cover = meta[pid]
        seqs = [v[0] for v in vs]
        key = {seqs[0], seqs[-1]} | ({current, cover} & set(seqs))
        for seq, outputs, mapping in vs:
            finals = [p for p in FITS_PORTS[1:] if outputs.get(p)]
            ports = ["image", "stacked", *finals[:1]] if seq in key else ["image"]
            for port in ports:
                ref = outputs.get(port)
                if not isinstance(ref, dict) or not ref.get("node_hash"):
                    continue
                source = mapping.get(port) if isinstance(mapping, dict) else None
                node_port = str(source).split(".", 1)[1] if source and "." in str(source) else port
                out.setdefault(
                    str(ref["node_hash"]),
                    Wanted(
                        project=name,
                        seq=seq,
                        port=port,
                        node_port=node_port,
                        path=str(ref.get("path") or ""),
                        type=str(ref.get("type") or ""),
                        display_ready=bool(ref.get("display_ready")),
                    ),
                )
    return out


def _from_nas(cache: ContentCache, h: str, w: Wanted, images: Path) -> bool:
    """Rebuild a kept entry from its NAS backup. These nodes have one output
    port, so the one backed-up file makes a whole entry."""
    suffix = Path(w.path).suffix or (".png" if w.port == "image" else ".fit")
    src = image_backup_path(images, w.project, w.seq, w.port, suffix)
    if not src.is_file():
        return False
    tmp = cache.kept_root / f".nas-{h}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        name = Path(w.path).name or f"{w.node_port}{suffix}"
        shutil.copy2(src, tmp / name)
        manifest = {w.node_port: {"path": name, "type": w.type, "display_ready": w.display_ready}}
        (tmp / OUTPUTS_MANIFEST).write_text(json.dumps(manifest, indent=2))
        (tmp / DONE_MARKER).touch()
        tmp.rename(cache.kept_dir(h))
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        log.warning("kept results: could not copy %s from the NAS: %s", src, exc)
        return False
    return True


def reconcile(
    cache: ContentCache, conn: sqlite3.Connection, nas_images: Path | None = None
) -> dict[str, int]:
    """Bring the kept results in line with `wanted`: link new ones from the
    cache, copy missing ones from the NAS, drop the rest."""
    want = wanted(conn)
    kept_root = cache.kept_root
    kept_root.mkdir(parents=True, exist_ok=True)
    stats = {"kept": 0, "linked": 0, "from_nas": 0, "missing": 0, "dropped": 0}
    for h, w in want.items():
        if (cache.kept_dir(h) / DONE_MARKER).exists():
            stats["kept"] += 1
            continue
        live = cache.entry_dir(h)
        if (live / DONE_MARKER).exists():
            tmp = kept_root / f".link-{h}"
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                link_tree(live, tmp)
                tmp.rename(cache.kept_dir(h))
            except OSError as exc:
                shutil.rmtree(tmp, ignore_errors=True)
                log.warning("kept results: could not keep %s: %s", h[:12], exc)
                stats["missing"] += 1
                continue
            stats["linked"] += 1
        elif nas_images is not None and _from_nas(cache, h, w, nas_images):
            stats["from_nas"] += 1
        else:
            stats["missing"] += 1
            continue
        stats["kept"] += 1
    for child in kept_root.iterdir():
        if child.name in want:
            continue
        shutil.rmtree(child, ignore_errors=True)
        stats["dropped"] += 1
    if stats["linked"] or stats["from_nas"] or stats["dropped"]:
        log.info("kept results: %s", stats)
    return stats
