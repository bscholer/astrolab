"""Tests for `server.system_metrics` and the `/api/system` endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.api import app
from server.jobs import JobEvent, JobRecord
from server.models import Job, NodeSpec, Template


def _template(node_ids: tuple[str, ...] = ("a", "b", "c")) -> Template:
    return Template(
        id="t-test",
        version=1,
        description="Test pipeline",
        nodes=[NodeSpec(id=nid, kind="noop") for nid in node_ids],
    )


def _job(target_id: str | None = None) -> Job:
    return Job(template_id="t-test", template_version=1, target_id=target_id)


def _record(
    *,
    job_id: str,
    status: str,
    finished_at: str | None = None,
    started_at: str | None = None,
    events: list[JobEvent] | None = None,
    template: Template | None = None,
) -> JobRecord:
    return JobRecord(
        id=job_id,
        status=status,  # type: ignore[arg-type]
        template=template if template is not None else _template(),
        job=_job(),
        submitted_at="2026-05-10T00:00:00+00:00",
        started_at=started_at,
        finished_at=finished_at,
        events=events or [],
    )


class FakeCache:
    def __init__(self, root: Path) -> None:
        self.root = root


class FakeJobManager:
    def __init__(self, records: list[JobRecord], cache_root: Path) -> None:
        self._records = records
        self.cache = FakeCache(cache_root)
        self.db_path: Path | None = None

    def list_jobs(self) -> list[JobRecord]:
        return list(self._records)


def test_payload_shape_matches_contract(tmp_path: Path) -> None:
    from server.system_metrics import collect_system_metrics

    jm = FakeJobManager([], cache_root=tmp_path)
    # Force the GPU probe to report "absent" so we don't depend on the host.
    with patch("server.system_metrics._nvidia_smi_query", return_value=None):
        payload = collect_system_metrics(jm)  # type: ignore[arg-type]

    assert set(payload.keys()) == {"host", "cpu", "mem", "disk", "gpu", "jobs", "sampled_at"}

    host = payload["host"]
    assert isinstance(host["hostname"], str)
    assert isinstance(host["os"], str)
    assert isinstance(host["kernel"], str)
    assert isinstance(host["cpu_model"], str)
    assert isinstance(host["cores"], int) and host["cores"] >= 1
    assert isinstance(host["ram_total"], int)
    assert isinstance(host["disk_total"], int)
    assert host["gpu_model"] is None or isinstance(host["gpu_model"], str)
    assert isinstance(host["uptime_s"], int)

    cpu = payload["cpu"]
    assert isinstance(cpu["percent"], float)
    assert isinstance(cpu["per_core"], list)
    assert len(cpu["per_core"]) == host["cores"]
    assert all(isinstance(v, float) for v in cpu["per_core"])
    assert isinstance(cpu["load_avg"], list) and len(cpu["load_avg"]) == 3
    assert cpu["temp_c"] is None or isinstance(cpu["temp_c"], float)

    mem = payload["mem"]
    assert isinstance(mem["used"], int)
    assert isinstance(mem["total"], int)
    assert isinstance(mem["swap_used"], int)

    disk = payload["disk"]
    assert isinstance(disk["mount"], str)
    assert isinstance(disk["used"], int)
    assert isinstance(disk["total"], int)
    assert isinstance(disk["read_bps"], int)
    assert isinstance(disk["write_bps"], int)

    assert payload["gpu"] is None

    jobs = payload["jobs"]
    assert isinstance(jobs["queued"], int)
    assert isinstance(jobs["running"], int)
    assert isinstance(jobs["completed_24h"], int)
    assert isinstance(jobs["failed_24h"], int)
    assert isinstance(jobs["active"], list)
    assert isinstance(jobs["throughput_6h"], list)
    assert len(jobs["throughput_6h"]) == 12
    assert all(isinstance(v, int) for v in jobs["throughput_6h"])

    assert isinstance(payload["sampled_at"], int)


def test_throughput_buckets_finished_jobs(tmp_path: Path) -> None:
    from server.system_metrics import collect_system_metrics

    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)

    # Drop jobs into known half-hour buckets within the 6h window. Bucket 0 is
    # the oldest (~5.5h-6h ago); bucket 11 is the newest (last 30 min).
    def at(minutes_ago: int) -> str:
        return (now - timedelta(minutes=minutes_ago)).isoformat()

    records = [
        _record(job_id="j1", status="completed", finished_at=at(15)),  # bucket 11
        _record(job_id="j2", status="completed", finished_at=at(25)),  # bucket 11
        _record(job_id="j3", status="failed", finished_at=at(45)),  # bucket 10
        _record(job_id="j4", status="completed", finished_at=at(125)),  # bucket 7
        _record(job_id="j5", status="interrupted", finished_at=at(20)),  # ignored
        _record(job_id="j6", status="completed", finished_at=at(400)),  # >6h, ignored
    ]
    jm = FakeJobManager(records, cache_root=tmp_path)

    with (
        patch("server.system_metrics._now_utc", return_value=now),
        patch("server.system_metrics._nvidia_smi_query", return_value=None),
    ):
        payload = collect_system_metrics(jm)  # type: ignore[arg-type]

    throughput = payload["jobs"]["throughput_6h"]
    assert len(throughput) == 12
    assert throughput[11] == 2  # j1 + j2
    assert throughput[10] == 1  # j3
    assert throughput[7] == 1  # j4
    # All other buckets empty.
    for i, v in enumerate(throughput):
        if i in (7, 10, 11):
            continue
        assert v == 0, f"bucket {i} should be empty, got {v}"

    jobs = payload["jobs"]
    # 24h counters: j1/j2/j4/j6 completed (all within 24h), j3 failed (within 24h).
    assert jobs["completed_24h"] == 4
    assert jobs["failed_24h"] == 1


def test_active_jobs_progress_from_events(tmp_path: Path) -> None:
    from server.system_metrics import collect_system_metrics

    tpl = _template(("n1", "n2", "n3", "n4"))
    running = _record(
        job_id="r1",
        status="running",
        started_at="2026-05-10T11:50:00+00:00",
        template=tpl,
        events=[
            JobEvent(type="node_completed", timestamp="t", node_id="n1"),
            JobEvent(type="node_completed", timestamp="t", node_id="n2"),
        ],
    )
    queued = _record(job_id="q1", status="queued", template=tpl)
    jm = FakeJobManager([running, queued], cache_root=tmp_path)

    with patch("server.system_metrics._nvidia_smi_query", return_value=None):
        payload = collect_system_metrics(jm)  # type: ignore[arg-type]

    by_id = {a["id"]: a for a in payload["jobs"]["active"]}
    assert pytest.approx(by_id["r1"]["progress"], abs=1e-9) == 0.5
    assert by_id["q1"]["progress"] == 0.0
    assert by_id["r1"]["template_name"] == "Test pipeline"
    assert by_id["r1"]["target_name"] is None
    assert payload["jobs"]["running"] == 1
    assert payload["jobs"]["queued"] == 1


def test_gpu_null_when_nvidia_smi_missing(tmp_path: Path) -> None:
    from server import system_metrics

    jm = FakeJobManager([], cache_root=tmp_path)

    # Force a fresh probe: clear the latch, simulate FileNotFoundError.
    system_metrics._GPU_AVAILABLE = None
    with patch(
        "server.system_metrics.subprocess.run",
        side_effect=FileNotFoundError("nvidia-smi not on PATH"),
    ):
        payload = system_metrics.collect_system_metrics(jm)  # type: ignore[arg-type]

    assert payload["gpu"] is None
    assert payload["host"]["gpu_model"] is None
    # Latch should now be False so we don't keep retrying.
    assert system_metrics._GPU_AVAILABLE is False


def test_endpoint_smoke(tmp_path: Path) -> None:
    client = TestClient(app)
    r = client.get("/api/system")
    assert r.status_code == 200
    body = r.json()
    assert {"host", "cpu", "mem", "disk", "gpu", "jobs", "sampled_at"} <= set(body.keys())
    assert isinstance(body["jobs"]["throughput_6h"], list)
    assert len(body["jobs"]["throughput_6h"]) == 12
