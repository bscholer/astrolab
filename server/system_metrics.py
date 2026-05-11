"""Host telemetry for the dashboard's `/api/system` endpoint.

Read-only sampler. State carried across calls is intentionally tiny:
the previous `psutil.disk_io_counters()` snapshot (to compute byte
rates) and a cached negative result for nvidia-smi so we don't keep
forking shells on every poll.
"""

from __future__ import annotations

import platform
import shutil
import socket
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from .catalog.db import connect as open_catalog_db

if TYPE_CHECKING:
    from .jobs import JobManager

# psutil's first cpu_percent() call after import returns 0.0; priming it
# at import time means the first endpoint hit reports a real value.
psutil.cpu_percent(interval=None)
psutil.cpu_percent(interval=None, percpu=True)

_DISK_IO_PREV: dict[str, tuple[int, int, float]] = {}
"""mount_path -> (read_bytes, write_bytes, monotonic_ts) from last sample."""

_GPU_AVAILABLE: bool | None = None
"""Tri-state: None=unprobed, True=nvidia-smi works, False=skip from now on."""

_TARGET_NAME_CACHE: dict[tuple[str | None, str], str | None] = {}
"""(db_path_str_or_None, target_id) -> resolved name. Cleared on rehydrate is
unnecessary: target renames are rare and the cache is per-process anyway."""


def _host_block() -> dict[str, Any]:
    uname = platform.uname()
    vm = psutil.virtual_memory()
    cores = psutil.cpu_count(logical=True) or 1
    try:
        cpu_freq = psutil.cpu_freq()
        cpu_model = uname.processor or (cpu_freq.max and f"{cpu_freq.max} MHz") or "unknown"
    except Exception:
        cpu_model = uname.processor or "unknown"

    # platform.mac_ver()/freedesktop_os_release for a friendly OS string;
    # fall back to raw release if neither yields anything useful.
    os_str = f"{uname.system} {uname.release}"
    if uname.system == "Darwin":
        mac_ver = platform.mac_ver()[0]
        if mac_ver:
            os_str = f"macOS {mac_ver}"
    elif uname.system == "Linux":
        try:
            info = platform.freedesktop_os_release()
            if info.get("PRETTY_NAME"):
                os_str = info["PRETTY_NAME"]
        except (AttributeError, OSError):
            pass

    return {
        "hostname": socket.gethostname(),
        "os": os_str,
        "kernel": uname.release,
        "cpu_model": cpu_model,
        "cores": int(cores),
        "ram_total": int(vm.total),
        "disk_total": int(shutil.disk_usage("/").total),
        "gpu_model": _gpu_model_name(),
        "uptime_s": int(time.time() - psutil.boot_time()),
    }


def _cpu_block(core_count: int) -> dict[str, Any]:
    percent = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    # Defensive: psutil can return a different length than cpu_count on rare
    # platforms; the contract demands per_core length == host.cores.
    if len(per_core) != core_count:
        per_core = (per_core + [0.0] * core_count)[:core_count]
    try:
        load1, load5, load15 = psutil.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = 0.0
    return {
        "percent": float(percent),
        "per_core": [float(x) for x in per_core],
        "load_avg": [float(load1), float(load5), float(load15)],
        "temp_c": _cpu_temp(),
    }


def _cpu_temp() -> float | None:
    sensors_fn = getattr(psutil, "sensors_temperatures", None)
    if sensors_fn is None:
        return None
    try:
        temps = sensors_fn()
    except Exception:
        return None
    if not temps:
        return None
    for key in ("coretemp", "cpu_thermal", "k10temp", "cpu-thermal"):
        if key in temps and temps[key]:
            return float(temps[key][0].current)
    # Last-ditch: take the first sensor reading we can find.
    for readings in temps.values():
        if readings:
            return float(readings[0].current)
    return None


def _mem_block() -> dict[str, int]:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "used": int(vm.used),
        "total": int(vm.total),
        "swap_used": int(sw.used),
    }


def _disk_block(working_path: Path) -> dict[str, Any]:
    # Use the cache root's mount for usage and I/O so the dashboard reflects
    # whatever volume the pipeline actually writes to. shutil.disk_usage works
    # on macOS+Linux and is cheap.
    try:
        usage = shutil.disk_usage(working_path)
        total = int(usage.total)
        used = int(usage.used)
    except OSError:
        total = 0
        used = 0

    # psutil.disk_io_counters() returns aggregate counters across all disks
    # when perdisk=False. For "the working volume" we'd need to map mount ->
    # device, which is fiddly on macOS; aggregating is good enough for a
    # status indicator and the contract treats it as a scalar rate.
    read_bps = 0
    write_bps = 0
    try:
        io = psutil.disk_io_counters()
    except Exception:
        io = None
    if io is not None:
        now = time.monotonic()
        key = "__aggregate__"
        prev = _DISK_IO_PREV.get(key)
        if prev is not None:
            prev_read, prev_write, prev_ts = prev
            dt = now - prev_ts
            if dt > 0:
                read_bps = max(0, int((io.read_bytes - prev_read) / dt))
                write_bps = max(0, int((io.write_bytes - prev_write) / dt))
        _DISK_IO_PREV[key] = (io.read_bytes, io.write_bytes, now)

    return {
        "mount": str(working_path),
        "used": used,
        "total": total,
        "read_bps": read_bps,
        "write_bps": write_bps,
    }


def _gpu_model_name() -> str | None:
    info = _nvidia_smi_query()
    if info is None:
        return None
    return info["model"]


def _gpu_block() -> dict[str, Any] | None:
    info = _nvidia_smi_query()
    if info is None:
        return None
    return info["block"]


def _nvidia_smi_query() -> dict[str, Any] | None:
    """Shell out to nvidia-smi once and return a parsed payload, or None.

    The first failed call latches `_GPU_AVAILABLE=False` so subsequent polls
    skip the subprocess entirely. macOS dev boxes pay this cost exactly once.
    """
    global _GPU_AVAILABLE
    if _GPU_AVAILABLE is False:
        return None
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _GPU_AVAILABLE = False
        return None
    if result.returncode != 0 or not result.stdout.strip():
        _GPU_AVAILABLE = False
        return None
    line = result.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 5:
        _GPU_AVAILABLE = False
        return None
    try:
        name = parts[0]
        util = float(parts[1])
        vram_used_mib = float(parts[2])
        vram_total_mib = float(parts[3])
        temp_c = float(parts[4])
    except ValueError:
        _GPU_AVAILABLE = False
        return None
    _GPU_AVAILABLE = True
    return {
        "model": name,
        "block": {
            "model": name,
            "util": util,
            "vram_used": int(vram_used_mib * 1024 * 1024),
            "vram_total": int(vram_total_mib * 1024 * 1024),
            "temp_c": temp_c,
        },
    }


def _resolve_target_name(target_id: str | None, db_path: Path | None) -> str | None:
    if not target_id:
        return None
    cache_key = (str(db_path) if db_path else None, target_id)
    if cache_key in _TARGET_NAME_CACHE:
        return _TARGET_NAME_CACHE[cache_key]
    name: str | None = None
    try:
        conn = open_catalog_db(db_path)
        try:
            row = conn.execute("SELECT name FROM targets WHERE id = ?", (target_id,)).fetchone()
            if row is not None:
                name = row["name"]
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        name = None
    if name is None:
        # Fall back to the raw id so the UI has something to render rather
        # than an empty cell on legacy or pre-scan records.
        name = str(target_id)
    _TARGET_NAME_CACHE[cache_key] = name
    return name


def _job_progress(record: Any, job_manager: Any | None = None) -> float:
    """Cheap completed-nodes / total-nodes ratio.

    The runtime emits `node_completed` once per finished node; counting them
    against the template's declared node list gives a monotonically increasing
    fraction without scanning the (more verbose) `node_progress` stream. v1
    accepts the coarse-grained value; finer-grained progress can layer in
    later if anyone asks.

    Events are read from `record.events` if populated (legacy/test code
    that synthesises records inline) and otherwise pulled from the job
    manager - `get()` no longer eager-loads them now that submit/execute
    cross a process boundary.
    """
    try:
        total = len(record.template.nodes)
    except Exception:
        return 0.0
    if total <= 0:
        return 0.0
    events = list(getattr(record, "events", []) or [])
    if not events and job_manager is not None:
        try:
            events = job_manager.get_events(record.id)
        except Exception:
            events = []
    completed = 0
    seen_nodes: set[str] = set()
    for ev in events:
        if (
            ev.type in ("node_completed", "node_cached")
            and ev.node_id is not None
            and ev.node_id not in seen_nodes
        ):
            seen_nodes.add(ev.node_id)
            completed += 1
    return min(1.0, completed / total)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _jobs_block(job_manager: JobManager, now: datetime) -> dict[str, Any]:
    records = job_manager.list_jobs()
    queued = 0
    running = 0
    completed_24h = 0
    failed_24h = 0
    active: list[dict[str, Any]] = []
    bucket_count = 12
    bucket_width_s = 30 * 60
    window_s = bucket_count * bucket_width_s
    window_start = now.timestamp() - window_s
    throughput = [0] * bucket_count

    try:
        db_path = job_manager.db_path
    except Exception:
        db_path = None

    for r in records:
        if r.status == "queued":
            queued += 1
        elif r.status == "running":
            running += 1

        if r.status in ("queued", "running"):
            started_iso = r.started_at
            active.append(
                {
                    "id": r.id,
                    "target_name": _resolve_target_name(r.job.target_id, db_path),
                    "template_name": r.template.description or r.template.id,
                    "started_at": started_iso,
                    "progress": _job_progress(r, job_manager) if r.status == "running" else 0.0,
                }
            )

        finished_dt = _parse_iso(r.finished_at)
        if finished_dt is not None:
            finished_ts = finished_dt.timestamp()
            age = now.timestamp() - finished_ts
            if 0 <= age <= 24 * 3600:
                if r.status == "completed":
                    completed_24h += 1
                elif r.status == "failed":
                    failed_24h += 1
            if finished_ts >= window_start and r.status in ("completed", "failed"):
                idx = int((finished_ts - window_start) // bucket_width_s)
                if idx < 0:
                    idx = 0
                elif idx >= bucket_count:
                    idx = bucket_count - 1
                throughput[idx] += 1

    return {
        "queued": queued,
        "running": running,
        "completed_24h": completed_24h,
        "failed_24h": failed_24h,
        "active": active,
        "throughput_6h": throughput,
    }


def _now_utc() -> datetime:
    return datetime.now(UTC)


def collect_system_metrics(job_manager: JobManager) -> dict[str, Any]:
    """Return the full `/api/system` payload.

    Pure read of psutil + JobManager state. Safe to call from a request
    handler; the only mutable state mutated is `_DISK_IO_PREV` (for byte-rate
    deltas) and the lazy GPU/target-name caches.
    """
    now = _now_utc()
    host = _host_block()
    cpu = _cpu_block(host["cores"])
    mem = _mem_block()
    working_path = job_manager.cache.root
    disk = _disk_block(working_path)
    gpu = _gpu_block()
    jobs = _jobs_block(job_manager, now)

    return {
        "host": host,
        "cpu": cpu,
        "mem": mem,
        "disk": disk,
        "gpu": gpu,
        "jobs": jobs,
        "sampled_at": int(now.timestamp()),
    }
