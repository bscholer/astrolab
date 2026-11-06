"""Prometheus text exposition for `/metrics`.

Cheap on purpose: a few SQL counts and one statvfs. It never walks the cache
tree, which can hold terabytes.
"""

from __future__ import annotations

import shutil
from collections import Counter
from typing import TYPE_CHECKING

from .catalog.db import connect as open_catalog_db
from .storage import SETTING_CACHE_MAX_BYTES, default_cache_max_bytes_for, get_setting

if TYPE_CHECKING:
    from .jobs import JobManager

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

JOB_STATUSES = ("queued", "running", "completed", "failed", "interrupted")
CATALOG_TABLES = ("targets", "frames", "sessions", "projects", "masters")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(name: str, value: float, **labels: str) -> str:
    if labels:
        body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        return f"{name}{{{body}}} {value}"
    return f"{name} {value}"


def _family(name: str, help_text: str, samples: list[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", *samples]


def render_metrics(job_manager: JobManager) -> str:
    db_path = job_manager.db_path
    lines: list[str] = []

    counts = Counter(r.status for r in job_manager.list_jobs())
    lines += _family(
        "astrolab_jobs",
        "Jobs by status, across the whole job history.",
        [_sample("astrolab_jobs", counts.get(s, 0), status=s) for s in JOB_STATUSES],
    )

    conn = open_catalog_db(db_path)
    try:
        rows = [
            _sample(
                "astrolab_catalog_rows",
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],  # noqa: S608
                table=table,
            )
            for table in CATALOG_TABLES
        ]
    finally:
        conn.close()
    lines += _family("astrolab_catalog_rows", "Rows in the catalog database.", rows)

    root = job_manager.cache.root
    try:
        usage = shutil.disk_usage(root)
        used, total = usage.used, usage.total
    except OSError:
        used = total = 0
    path = str(root)
    lines += _family(
        "astrolab_cache_disk_used_bytes",
        "Used bytes on the filesystem that holds the cache.",
        [_sample("astrolab_cache_disk_used_bytes", used, path=path)],
    )
    lines += _family(
        "astrolab_cache_disk_total_bytes",
        "Size in bytes of the filesystem that holds the cache.",
        [_sample("astrolab_cache_disk_total_bytes", total, path=path)],
    )
    budget = int(
        get_setting(SETTING_CACHE_MAX_BYTES, default_cache_max_bytes_for(root), db_path=db_path)
    )
    lines += _family(
        "astrolab_cache_budget_bytes",
        "Configured cache size budget in bytes.",
        [_sample("astrolab_cache_budget_bytes", budget)],
    )
    return "\n".join(lines) + "\n"
