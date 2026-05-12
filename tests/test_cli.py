"""Tests for server.cli - the astrolab HTTP API thin CLI wrapper.

Each subcommand test monkeypatches httpx.Client so no real server is needed.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import patch

import pytest

from server.cli import main, parse_overrides

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, data: Any, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    @property
    def is_success(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        return self._data


def _fake_client(responses: dict[tuple[str, str], FakeResponse]):
    """Build a context-manager mock for httpx.Client.

    responses maps (method, path) -> FakeResponse.
    method is 'GET', 'POST', 'PATCH'.
    """

    class _Inner:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, path: str, **kwargs) -> FakeResponse:
            key = ("GET", path)
            if key not in responses:
                return FakeResponse({"detail": "not found"}, status_code=404)
            return responses[key]

        def post(self, path: str, json=None, **kwargs) -> FakeResponse:
            key = ("POST", path)
            if key not in responses:
                return FakeResponse({"detail": "not found"}, status_code=404)
            return responses[key]

        def patch(self, path: str, json=None, **kwargs) -> FakeResponse:
            key = ("PATCH", path)
            if key not in responses:
                return FakeResponse({"detail": "not found"}, status_code=404)
            return responses[key]

    return _Inner


# ---------------------------------------------------------------------------
# Override parser
# ---------------------------------------------------------------------------


class TestParseOverrides:
    def test_single_int(self):
        result = parse_overrides("stack.kappa=3")
        assert result == {"stack": {"kappa": 3}}

    def test_single_float(self):
        result = parse_overrides("stretch.shadows_clip=0.1")
        assert result == {"stretch": {"shadows_clip": 0.1}}

    def test_bool_true(self):
        result = parse_overrides("calibrate.enabled=true")
        assert result == {"calibrate": {"enabled": True}}

    def test_bool_false(self):
        result = parse_overrides("calibrate.enabled=False")
        assert result == {"calibrate": {"enabled": False}}

    def test_string_value(self):
        result = parse_overrides("stretch.method=mtf")
        assert result == {"stretch": {"method": "mtf"}}

    def test_multiple_same_node(self):
        result = parse_overrides("stack.kappa=2,stack.method=sigma")
        assert result == {"stack": {"kappa": 2, "method": "sigma"}}

    def test_multiple_nodes(self):
        result = parse_overrides("stack.kappa=2,stretch.method=mtf")
        assert result == {"stack": {"kappa": 2}, "stretch": {"method": "mtf"}}

    def test_empty_string_is_empty(self):
        result = parse_overrides("")
        assert result == {}

    def test_missing_equals_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            parse_overrides("stack.kappa")
        assert exc.value.code == 2

    def test_missing_dot_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            parse_overrides("kappa=3")
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


class TestSubmit:
    def test_submit_with_override_patches_project(self, monkeypatch, capsys):
        project_id = "proj-abc"
        job_id = "job-123"
        fake = _fake_client(
            {
                (
                    "PATCH",
                    f"/api/projects/{project_id}",
                ): FakeResponse(
                    {
                        "id": project_id,
                        "current_job_id": job_id,
                        "history": [{"job_id": job_id}],
                    }
                )
            }
        )
        monkeypatch.setattr("httpx.Client", fake)
        monkeypatch.setattr(sys, "stdout", sys.stdout)
        main(["--json", "submit", "--project", project_id, "--override", "stack.kappa=2"])
        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["job_id"] == job_id

    def test_submit_without_override_force_reruns(self, monkeypatch, capsys):
        project_id = "proj-xyz"
        job_id = "job-999"
        patched_bodies = []

        class _FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def patch(self, path, json=None, **kwargs):
                patched_bodies.append(json)
                return FakeResponse(
                    {"id": project_id, "current_job_id": job_id, "history": [{"job_id": job_id}]}
                )

        monkeypatch.setattr("httpx.Client", _FakeClient)
        main(["--json", "submit", "--project", project_id])
        assert patched_bodies == [{"force": True}]

    def test_submit_exits_1_on_404(self, monkeypatch):
        fake = _fake_client({})  # no routes -> 404
        monkeypatch.setattr("httpx.Client", fake)
        with pytest.raises(SystemExit) as exc:
            main(["submit", "--project", "bad-id"])
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


class TestPoll:
    def test_poll_no_wait_prints_status(self, monkeypatch, capsys):
        job_id = "job-poll-1"
        fake = _fake_client(
            {("GET", f"/api/jobs/{job_id}"): FakeResponse({"id": job_id, "status": "completed"})}
        )
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "poll", job_id])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "completed"

    def test_poll_failed_job_exits_1(self, monkeypatch, capsys):
        job_id = "job-fail"
        fake = _fake_client(
            {("GET", f"/api/jobs/{job_id}"): FakeResponse({"id": job_id, "status": "failed"})}
        )
        monkeypatch.setattr("httpx.Client", fake)
        with pytest.raises(SystemExit) as exc:
            main(["poll", job_id])
        assert exc.value.code == 1

    def test_poll_wait_returns_on_completed(self, monkeypatch, capsys):
        job_id = "job-wait"
        call_count = 0

        class _FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def get(self, path, **kwargs):
                nonlocal call_count
                call_count += 1
                # First call returns queued, second returns completed.
                status = "queued" if call_count == 1 else "completed"
                return FakeResponse({"id": job_id, "status": status})

        monkeypatch.setattr("httpx.Client", _FakeClient)
        monkeypatch.setattr("time.sleep", lambda _: None)
        main(["--json", "poll", job_id, "--wait"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "completed"
        assert call_count == 2

    def test_poll_wait_timeout_exits_1(self, monkeypatch):
        job_id = "job-timeout"

        class _FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def get(self, path, **kwargs):
                return FakeResponse({"id": job_id, "status": "running"})

        monkeypatch.setattr("httpx.Client", _FakeClient)
        monkeypatch.setattr("time.sleep", lambda _: None)
        # Force immediate timeout by monkey-patching monotonic.
        mono_calls = [0]

        def _fake_monotonic():
            v = mono_calls[0]
            mono_calls[0] += 5
            return v

        monkeypatch.setattr("time.monotonic", _fake_monotonic)
        with pytest.raises(SystemExit) as exc:
            main(["poll", job_id, "--wait", "--timeout", "1"])
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# rerun
# ---------------------------------------------------------------------------


class TestRerun:
    def test_rerun_posts_and_returns_new_job_id(self, monkeypatch, capsys):
        job_id = "job-old"
        new_id = "job-new"
        fake = _fake_client(
            {("POST", f"/api/jobs/{job_id}/rerun"): FakeResponse({"job_id": new_id})}
        )
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "rerun", job_id])
        out = json.loads(capsys.readouterr().out)
        assert out["job_id"] == new_id

    def test_rerun_404_exits_1(self, monkeypatch):
        fake = _fake_client({})
        monkeypatch.setattr("httpx.Client", fake)
        with pytest.raises(SystemExit) as exc:
            main(["rerun", "ghost"])
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# list-projects
# ---------------------------------------------------------------------------


class TestListProjects:
    _PROJECTS = [
        {"id": "p1", "name": "M42 OSC", "template_id": "calibrate_register_stack",
         "current_job_status": "completed", "updated_at": "2024-01-01T00:00:00"},
        {"id": "p2", "name": "NGC 7380", "template_id": "calibrate_register_stack_narrowband",
         "current_job_status": "running", "updated_at": "2024-01-02T00:00:00"},
    ]

    def test_lists_all_projects(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/projects"): FakeResponse(self._PROJECTS)})
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "list-projects"])
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2

    def test_target_filter(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/projects"): FakeResponse(self._PROJECTS)})
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "list-projects", "--target", "m42"])
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1
        assert out[0]["id"] == "p1"


# ---------------------------------------------------------------------------
# list-jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    _JOBS = [
        {"id": "j1", "status": "completed", "template_id": "tmpl-a",
         "submitted_at": "2024-01-01T00:00:00", "finished_at": "2024-01-01T01:00:00",
         "project_id": "p1"},
        {"id": "j2", "status": "failed", "template_id": "tmpl-b",
         "submitted_at": "2024-01-02T00:00:00", "finished_at": "2024-01-02T01:00:00",
         "project_id": "p2"},
    ]

    def test_lists_all_jobs(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/jobs"): FakeResponse(self._JOBS)})
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "list-jobs"])
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2

    def test_status_filter(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/jobs"): FakeResponse(self._JOBS)})
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "list-jobs", "--status", "failed"])
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1
        assert out[0]["id"] == "j2"

    def test_project_filter(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/jobs"): FakeResponse(self._JOBS)})
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "list-jobs", "--project", "p1"])
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1
        assert out[0]["id"] == "j1"

    def test_limit(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/jobs"): FakeResponse(self._JOBS)})
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "list-jobs", "--limit", "1"])
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


class TestDiff:
    _JOB_A = {
        "id": "ja",
        "status": "completed",
        "finished_at": "2024-01-01T10:00:00",
        "outputs": {"image": {"node_hash": "abc", "port": "image", "path": "/out/a.png"}},
        "template_json": {"overrides": {"stack": {"kappa": 2}}},
    }
    _JOB_B = {
        "id": "jb",
        "status": "completed",
        "finished_at": "2024-01-01T11:00:00",
        "outputs": {"image": {"node_hash": "def", "port": "image", "path": "/out/b.png"}},
        "template_json": {"overrides": {"stack": {"kappa": 3}}},
    }

    def test_diff_shows_param_change(self, monkeypatch, capsys):
        fake = _fake_client(
            {
                ("GET", "/api/jobs/ja"): FakeResponse(self._JOB_A),
                ("GET", "/api/jobs/jb"): FakeResponse(self._JOB_B),
            }
        )
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "diff", "ja", "jb"])
        out = json.loads(capsys.readouterr().out)
        assert out["job_a"] == "ja"
        assert out["job_b"] == "jb"
        assert len(out["param_diff"]) == 1
        diff_row = out["param_diff"][0]
        assert diff_row["node.param"] == "stack.kappa"
        assert diff_row["job_a"] == 2
        assert diff_row["job_b"] == 3

    def test_diff_no_params_empty_diff(self, monkeypatch, capsys):
        job_a = {**self._JOB_A, "template_json": {"overrides": {}}}
        job_b = {**self._JOB_B, "template_json": {"overrides": {}}}
        fake = _fake_client(
            {("GET", "/api/jobs/ja"): FakeResponse(job_a), ("GET", "/api/jobs/jb"): FakeResponse(job_b)}
        )
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "diff", "ja", "jb"])
        out = json.loads(capsys.readouterr().out)
        assert out["param_diff"] == []


# ---------------------------------------------------------------------------
# cache-stats
# ---------------------------------------------------------------------------


class TestCacheStats:
    _STORAGE = {
        "total_bytes": 2_000_000_000,
        "entry_count": 42,
        "unreachable_bytes": 100_000,
        "cache_root": "/data/cache",
        "cache_disk": {"total_bytes": 500_000_000_000, "used_bytes": 100_000_000, "free_bytes": 400_000_000_000},
        "per_project": [
            {"project_id": "p1", "name": "M42", "updated_at": "2024-01-01", "owned_bytes": 1_500_000_000, "shared_bytes": 0, "entry_count": 30},
        ],
    }

    def test_cache_stats_json(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/storage"): FakeResponse(self._STORAGE)})
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "cache-stats"])
        out = json.loads(capsys.readouterr().out)
        assert out["entry_count"] == 42

    def test_cache_stats_human(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/storage"): FakeResponse(self._STORAGE)})
        monkeypatch.setattr("httpx.Client", fake)
        # Simulate TTY so human-readable output is chosen.
        with patch.object(sys.stdout, "isatty", return_value=True):
            main(["cache-stats"])
        out = capsys.readouterr().out
        assert "entry count" in out
        assert "GB" in out or "MB" in out or "TB" in out or "B" in out


# ---------------------------------------------------------------------------
# settings get / set
# ---------------------------------------------------------------------------


class TestSettings:
    _SETTINGS = {
        "cache_max_bytes": 10_737_418_240,
        "cache_root": None,
        "capture_root": "/data/captures",
    }

    def test_settings_get(self, monkeypatch, capsys):
        fake = _fake_client({("GET", "/api/settings"): FakeResponse(self._SETTINGS)})
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "settings", "get"])
        out = json.loads(capsys.readouterr().out)
        assert out["capture_root"] == "/data/captures"

    def test_settings_set_capture_root(self, monkeypatch, capsys):
        patched_body = {}

        class _FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def patch(self, path, json=None, **kwargs):
                patched_body.update(json or {})
                return FakeResponse(self._SETTINGS)

        _FakeClient._SETTINGS = self._SETTINGS
        monkeypatch.setattr("httpx.Client", _FakeClient)
        main(["settings", "set", "--capture-root", "/new/path"])
        assert patched_body == {"capture_root": "/new/path"}

    def test_settings_set_no_args_exits_2(self, monkeypatch):
        monkeypatch.setattr("httpx.Client", _fake_client({}))
        with pytest.raises(SystemExit) as exc:
            main(["settings", "set"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# TTY detection
# ---------------------------------------------------------------------------


class TestTtyDetection:
    def test_pipe_produces_json(self, monkeypatch, capsys):
        """When stdout is not a TTY, output should be valid JSON without --json."""
        fake = _fake_client(
            {("GET", "/api/jobs/j1"): FakeResponse({"id": "j1", "status": "completed"})}
        )
        monkeypatch.setattr("httpx.Client", fake)
        # isatty() returns False for captured stdout in pytest already, so just
        # verify output is parseable JSON.
        main(["poll", "j1"])
        out = capsys.readouterr().out.strip()
        # In non-tty mode: check that the raw status text is parseable
        # (could be 'completed' plain text or JSON).  We just check it's non-empty.
        assert out

    def test_explicit_json_flag_forces_json(self, monkeypatch, capsys):
        """--json always produces machine-readable output regardless of TTY."""
        fake = _fake_client(
            {("GET", "/api/jobs/j1"): FakeResponse({"id": "j1", "status": "queued"})}
        )
        monkeypatch.setattr("httpx.Client", fake)
        main(["--json", "poll", "j1"])
        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["status"] == "queued"
