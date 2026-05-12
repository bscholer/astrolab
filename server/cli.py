"""astrolab CLI - thin wrapper around the astrolab HTTP API.

Invokable as:
  python -m server.cli <subcommand> [args]
  astrolab <subcommand> [args]             (via pyproject.toml [project.scripts])

Exit codes:
  0  success
  1  expected failure (job failed, item not found)
  2  usage error
  3  unexpected error (HTTP 5xx, network error)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _base() -> str:
    return os.environ.get("ASTROLAB_API", "http://localhost:8000")


def _client() -> Any:
    try:
        import httpx
    except ImportError:  # pragma: no cover
        _die("httpx is not installed; add it to your dev dependencies", code=3)
    return httpx.Client(base_url=_base(), timeout=30.0)


def _die(msg: str, code: int = 3) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _get(client: Any, path: str) -> Any:
    try:
        r = client.get(path)
    except Exception as exc:
        _die(f"network error: {exc}", code=3)
    if r.status_code == 404:
        _die(f"not found: {path}", code=1)
    if r.status_code >= 500:
        _die(f"server error {r.status_code}: {r.text}", code=3)
    if not r.is_success:
        _die(f"HTTP {r.status_code}: {r.text}", code=1)
    return r.json()


def _post(client: Any, path: str, body: dict | None = None) -> Any:
    try:
        r = client.post(path, json=body)
    except Exception as exc:
        _die(f"network error: {exc}", code=3)
    if r.status_code == 404:
        _die(f"not found: {path}", code=1)
    if r.status_code >= 500:
        _die(f"server error {r.status_code}: {r.text}", code=3)
    if not r.is_success:
        _die(f"HTTP {r.status_code}: {r.text}", code=1)
    return r.json()


def _patch(client: Any, path: str, body: dict) -> Any:
    try:
        r = client.patch(path, json=body)
    except Exception as exc:
        _die(f"network error: {exc}", code=3)
    if r.status_code == 404:
        _die(f"not found: {path}", code=1)
    if r.status_code >= 500:
        _die(f"server error {r.status_code}: {r.text}", code=3)
    if not r.is_success:
        _die(f"HTTP {r.status_code}: {r.text}", code=1)
    return r.json()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _want_json(args: argparse.Namespace) -> bool:
    """Return True when output should be machine-readable JSON."""
    return getattr(args, "json", False) or not sys.stdout.isatty()


def _emit(data: Any, args: argparse.Namespace) -> None:
    """Print data as JSON (non-TTY / --json) or pass to the table formatter."""
    if _want_json(args):
        print(json.dumps(data, indent=2, default=str))
    else:
        print(json.dumps(data, indent=2, default=str))


def _table(rows: list[dict], cols: list[str], args: argparse.Namespace) -> None:
    """Print a list of dicts as a human-friendly table, or JSON if needed."""
    if _want_json(args):
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        print("(no results)")
        return
    widths = {c: len(c) for c in cols}
    for row in rows:
        for c in cols:
            v = str(row.get(c, ""))
            widths[c] = max(widths[c], len(v))
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    print(header)
    print(sep)
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in cols))


# ---------------------------------------------------------------------------
# Override parser
# ---------------------------------------------------------------------------

def _parse_value(raw: str) -> Any:
    """Coerce a string value: try int, float, bool, else keep as str."""
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_overrides(spec: str) -> dict[str, dict[str, Any]]:
    """Parse 'node.param=value,...' into {'node': {'param': value}}.

    Rules:
    - Items are comma-separated.
    - Each item must be 'node_id.param_name=value'.
    - Values are coerced: true/false -> bool, integers -> int,
      floats -> float, everything else stays as str.
    - Multiple params for the same node are merged into a single dict.
    """
    result: dict[str, dict[str, Any]] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            _die(f"override {item!r} missing '='; expected node.param=value", code=2)
        key, _, raw_val = item.partition("=")
        if "." not in key:
            _die(
                f"override key {key!r} missing node prefix; expected node.param=value",
                code=2,
            )
        node_id, _, param = key.partition(".")
        node_id = node_id.strip()
        param = param.strip()
        if not node_id or not param:
            _die(f"malformed override key {key!r}; expected node.param=value", code=2)
        result.setdefault(node_id, {})[param] = _parse_value(raw_val.strip())
    return result


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_submit(args: argparse.Namespace) -> None:
    """PATCH overrides onto a project and report the resulting job_id."""
    with _client() as client:
        if args.override:
            overrides = parse_overrides(args.override)
            body = _patch(client, f"/api/projects/{args.project}", {"overrides": overrides})
        else:
            # Trigger a force-rerun of the current state.
            body = _patch(client, f"/api/projects/{args.project}", {"force": True})
        job_id = body.get("current_job_id") or body["history"][-1]["job_id"]
    if _want_json(args):
        print(json.dumps({"job_id": job_id}))
    else:
        print(job_id)


def cmd_poll(args: argparse.Namespace) -> None:
    """Print current job status; with --wait poll until terminal state."""
    with _client() as client:
        if not args.wait:
            data = _get(client, f"/api/jobs/{args.job_id}")
            status = data.get("status", "unknown")
            if _want_json(args):
                print(json.dumps({"job_id": args.job_id, "status": status}))
            else:
                print(status)
            if status == "failed":
                sys.exit(1)
            return

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            data = _get(client, f"/api/jobs/{args.job_id}")
            status = data.get("status", "unknown")
            if status in ("completed", "failed", "interrupted"):
                if _want_json(args):
                    print(json.dumps({"job_id": args.job_id, "status": status}))
                else:
                    print(status)
                if status == "failed":
                    sys.exit(1)
                return
            time.sleep(2)

    _die(f"timed out after {args.timeout}s waiting for job {args.job_id}", code=1)


def cmd_rerun(args: argparse.Namespace) -> None:
    """Submit a fresh cache-busting copy of an existing job."""
    with _client() as client:
        data = _post(client, f"/api/jobs/{args.job_id}/rerun")
    new_job_id = data["job_id"]
    if _want_json(args):
        print(json.dumps({"job_id": new_job_id}))
    else:
        print(new_job_id)


def cmd_list_projects(args: argparse.Namespace) -> None:
    with _client() as client:
        projects = _get(client, "/api/projects")
    if args.target:
        target_lower = args.target.lower()
        projects = [
            p
            for p in projects
            if target_lower in (p.get("name") or "").lower()
            or target_lower in (p.get("template_id") or "").lower()
        ]
    rows = [
        {
            "id": p["id"],
            "name": p.get("name", ""),
            "template_id": p.get("template_id", ""),
            "status": p.get("current_job_status", ""),
            "updated_at": (p.get("updated_at") or "")[:19],
        }
        for p in projects
    ]
    _table(rows, ["id", "name", "template_id", "status", "updated_at"], args)


def cmd_list_jobs(args: argparse.Namespace) -> None:
    with _client() as client:
        jobs = _get(client, "/api/jobs")
    if args.project:
        jobs = [j for j in jobs if j.get("project_id") == args.project]
    if args.status:
        jobs = [j for j in jobs if j.get("status") == args.status]
    if args.limit:
        jobs = jobs[: args.limit]
    rows = [
        {
            "id": j["id"],
            "status": j.get("status", ""),
            "template_id": j.get("template_id", ""),
            "submitted_at": (j.get("submitted_at") or "")[:19],
            "finished_at": (j.get("finished_at") or "")[:19],
        }
        for j in jobs
    ]
    _table(rows, ["id", "status", "template_id", "submitted_at", "finished_at"], args)


def cmd_diff(args: argparse.Namespace) -> None:
    """Compare params and key stats between two jobs."""
    with _client() as client:
        job_a = _get(client, f"/api/jobs/{args.job_a}")
        job_b = _get(client, f"/api/jobs/{args.job_b}")

    def _overrides(job: dict) -> dict:
        tmpl = job.get("template_json") or {}
        return tmpl.get("overrides") or job.get("current_overrides") or {}

    ov_a = _overrides(job_a)
    ov_b = _overrides(job_b)

    all_nodes = set(ov_a) | set(ov_b)
    param_diff: list[dict] = []
    for node in sorted(all_nodes):
        params_a = ov_a.get(node) or {}
        params_b = ov_b.get(node) or {}
        all_params = set(params_a) | set(params_b)
        for p in sorted(all_params):
            va = params_a.get(p, "<default>")
            vb = params_b.get(p, "<default>")
            if va != vb:
                param_diff.append({"node.param": f"{node}.{p}", "job_a": va, "job_b": vb})

    def _stat(job: dict) -> dict:
        outs = job.get("outputs") or {}
        out_path = ""
        if outs:
            first_ref = next(iter(outs.values()), None)
            if isinstance(first_ref, dict):
                out_path = first_ref.get("path", "")
        return {
            "status": job.get("status", ""),
            "output_path": out_path,
            "finished_at": (job.get("finished_at") or "")[:19],
        }

    stats_a = _stat(job_a)
    stats_b = _stat(job_b)

    result = {
        "job_a": args.job_a,
        "job_b": args.job_b,
        "param_diff": param_diff,
        "stats": {
            "job_a": stats_a,
            "job_b": stats_b,
        },
    }

    if _want_json(args):
        print(json.dumps(result, indent=2, default=str))
        return

    if not param_diff:
        print("(no param differences)")
    else:
        _table(param_diff, ["node.param", "job_a", "job_b"], args)

    print()
    stat_rows = [
        {"field": "status", "job_a": stats_a["status"], "job_b": stats_b["status"]},
        {
            "field": "output_path",
            "job_a": stats_a["output_path"],
            "job_b": stats_b["output_path"],
        },
        {
            "field": "finished_at",
            "job_a": stats_a["finished_at"],
            "job_b": stats_b["finished_at"],
        },
    ]
    _table(stat_rows, ["field", "job_a", "job_b"], args)


def cmd_cache_stats(args: argparse.Namespace) -> None:
    with _client() as client:
        data = _get(client, "/api/storage")

    total_bytes = data.get("total_bytes", 0)
    entry_count = data.get("entry_count", 0)
    unreachable = data.get("unreachable_bytes", 0)
    per_project = data.get("per_project") or []

    if _want_json(args):
        print(json.dumps(data, indent=2, default=str))
        return

    def _fmt_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024  # type: ignore[assignment]
        return f"{n:.1f} PB"

    print(f"total cache size : {_fmt_bytes(total_bytes)}")
    print(f"entry count      : {entry_count}")
    print(f"unreachable      : {_fmt_bytes(unreachable)}")
    print(f"cache root       : {data.get('cache_root', '')}")
    print()
    top = sorted(per_project, key=lambda p: p.get("owned_bytes", 0), reverse=True)[:10]
    if top:
        rows = [
            {
                "project": p.get("name", p.get("project_id", "")),
                "owned": _fmt_bytes(p.get("owned_bytes", 0)),
                "shared": _fmt_bytes(p.get("shared_bytes", 0)),
                "entries": str(p.get("entry_count", 0)),
            }
            for p in top
        ]
        _table(rows, ["project", "owned", "shared", "entries"], args)


def cmd_settings_get(args: argparse.Namespace) -> None:
    with _client() as client:
        data = _get(client, "/api/settings")
    _emit(data, args)


def cmd_settings_set(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {}
    if args.capture_root is not None:
        body["capture_root"] = args.capture_root
    if not body:
        _die("no settings supplied; pass --capture-root or other flags", code=2)
    with _client() as client:
        data = _patch(client, "/api/settings", body)
    _emit(data, args)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astrolab",
        description="CLI wrapper for the astrolab HTTP API.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Always emit JSON (default when stdout is not a TTY).",
    )

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # submit
    p_submit = sub.add_parser("submit", help="Apply overrides to a project and return a job_id.")
    p_submit.add_argument("--project", required=True, metavar="ID", help="Project ID.")
    p_submit.add_argument(
        "--override",
        metavar="node.param=value,...",
        default=None,
        help="Comma-separated overrides, e.g. 'stack.kappa=2.5,stretch.method=mtf'.",
    )

    # poll
    p_poll = sub.add_parser("poll", help="Print job status; optionally wait for completion.")
    p_poll.add_argument("job_id", metavar="JOB_ID")
    p_poll.add_argument("--wait", action="store_true", default=False, help="Poll until done.")
    p_poll.add_argument(
        "--timeout",
        type=int,
        default=1800,
        metavar="SECONDS",
        help="Max seconds to wait (default 1800).",
    )

    # rerun
    p_rerun = sub.add_parser("rerun", help="Submit a cache-busting copy of a job.")
    p_rerun.add_argument("job_id", metavar="JOB_ID")

    # list-projects
    p_lp = sub.add_parser("list-projects", help="List projects.")
    p_lp.add_argument(
        "--target",
        metavar="NAME",
        default=None,
        help="Filter by substring of project name or template ID.",
    )

    # list-jobs
    p_lj = sub.add_parser("list-jobs", help="List jobs.")
    p_lj.add_argument("--project", metavar="ID", default=None, help="Filter by project ID.")
    p_lj.add_argument("--status", metavar="STATUS", default=None, help="Filter by status.")
    p_lj.add_argument("--limit", type=int, default=None, metavar="N", help="Cap results.")

    # diff
    p_diff = sub.add_parser("diff", help="Compare params and stats of two jobs.")
    p_diff.add_argument("job_a", metavar="JOB_A")
    p_diff.add_argument("job_b", metavar="JOB_B")

    # cache-stats
    sub.add_parser("cache-stats", help="Show cache size, entry count, and top projects.")

    # settings get
    p_settings = sub.add_parser("settings", help="Read or update settings.")
    settings_sub = p_settings.add_subparsers(dest="settings_action", required=True)

    settings_sub.add_parser("get", help="Print current settings as JSON.")

    p_sset = settings_sub.add_parser("set", help="Update one or more settings.")
    p_sset.add_argument("--capture-root", metavar="PATH", default=None)

    return parser


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "submit": cmd_submit,
        "poll": cmd_poll,
        "rerun": cmd_rerun,
        "list-projects": cmd_list_projects,
        "list-jobs": cmd_list_jobs,
        "diff": cmd_diff,
        "cache-stats": cmd_cache_stats,
        "settings": lambda a: (
            cmd_settings_get(a) if a.settings_action == "get" else cmd_settings_set(a)
        ),
    }
    fn = dispatch.get(args.subcommand)
    if fn is None:  # pragma: no cover
        parser.print_help()
        sys.exit(2)
    fn(args)


if __name__ == "__main__":
    main()
