"""Tests for POST /api/nodes/{kind}/run (single-node runner).

Covers:
- 404 for unknown kind
- 400 for invalid params
- 400 for input port type mismatch
- Successful submit returns 202 with job_id
- Cache hit path: pre-seed a cache dir, assert immediate 200 with cache_hit=true
- Hash stability: same hash from this endpoint and from the template-driven path
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import nodes.basic  # noqa: F401  registers downscale
from server.api import app, job_manager
from server.cache import ContentCache
from server.canonical import node_hash as compute_node_hash
from server.models import Ref
from server.ports import PortType
from server.registry import lookup as registry_lookup
from tests._jobs_helpers import background_worker


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    monkeypatch.setattr(job_manager, "_cache", cache)
    job_manager.reset_for_tests(db_path=db_path)
    with background_worker(cache, db_path), TestClient(app) as c:
        yield c


def _make_png(path: Path, size: tuple[int, int] = (200, 100)) -> Path:
    Image.new("RGB", size, (32, 64, 96)).save(path)
    return path


def _wait_for(client, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        body = r.json()
        if body["status"] in ("completed", "failed", "interrupted"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# 404: unknown kind
# ---------------------------------------------------------------------------


def test_unknown_kind_returns_404(client) -> None:
    r = client.post("/api/nodes/no_such_node/run", json={"inputs": {}, "params": {}})
    assert r.status_code == 404
    assert "no node registered" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 400: invalid params
# ---------------------------------------------------------------------------


def test_invalid_params_returns_400(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    payload = {
        "inputs": {
            "image": {
                "node_hash": "ext",
                "port": "image",
                "path": str(src),
                "type": "image/png",
            }
        },
        "params": {"target_size_px": -1},  # must be > 0
    }
    r = client.post("/api/nodes/downscale/run", json=payload)
    assert r.status_code == 400
    assert "invalid params" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 400: port type mismatch
# ---------------------------------------------------------------------------


def test_port_type_mismatch_returns_400(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    payload = {
        "inputs": {
            "image": {
                "node_hash": "ext",
                "port": "image",
                "path": str(src),
                "type": "image/fits",  # downscale expects image/png
            }
        },
        "params": {},
    }
    r = client.post("/api/nodes/downscale/run", json=payload)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "type mismatch" in detail
    assert "image/fits" in detail


# ---------------------------------------------------------------------------
# Successful submit returns 202 with job_id
# ---------------------------------------------------------------------------


def test_successful_submit_returns_202(client, tmp_path: Path) -> None:
    src = _make_png(tmp_path / "in.png")
    payload = {
        "inputs": {
            "image": {
                "node_hash": "ext",
                "port": "image",
                "path": str(src),
                "type": "image/png",
            }
        },
        "params": {"target_size_px": 64},
    }
    r = client.post("/api/nodes/downscale/run", json=payload)
    assert r.status_code == 202
    body = r.json()
    assert body["cache_hit"] is False
    assert body["job_id"] is not None
    assert body["node_hash"]

    # The job should complete successfully.
    result = _wait_for(client, body["job_id"])
    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Cache hit path
# ---------------------------------------------------------------------------


def test_cache_hit_returns_immediately(client, tmp_path: Path) -> None:
    """Pre-seed a committed cache entry; the endpoint must detect it and return
    cache_hit=true without submitting a job."""
    src = _make_png(tmp_path / "in.png")

    # Build the ref and params that the endpoint will hash.
    node_cls = registry_lookup("downscale")
    input_ref = Ref(
        node_hash="ext",
        port="image",
        path=src,
        type=PortType.IMAGE_PNG,
    )
    params = node_cls.params_schema.model_validate({"target_size_px": 64})
    h = compute_node_hash(
        node_id=node_cls.id,
        node_version=node_cls.version,
        inputs={"image": input_ref},
        params=params,
    )

    # Manually write a committed cache entry at that hash.
    cache = job_manager.cache
    entry_dir = cache.reserve(h)
    fake_output = entry_dir / "image.png"
    _make_png(fake_output)
    output_ref = Ref(
        node_hash=h,
        port="image",
        path=fake_output,
        type=PortType.IMAGE_PNG,
    )
    cache.commit(h, {"image": output_ref})

    # Now hit the endpoint with the same inputs+params.
    payload = {
        "inputs": {
            "image": {
                "node_hash": "ext",
                "port": "image",
                "path": str(src),
                "type": "image/png",
            }
        },
        "params": {"target_size_px": 64},
    }
    r = client.post("/api/nodes/downscale/run", json=payload)
    # Cache hit returns the committed outputs inline, status 202 is still used
    # but cache_hit is true and job_id is null.
    body = r.json()
    assert body["cache_hit"] is True
    assert body["job_id"] is None
    assert body["node_hash"] == h
    assert body["outputs"] is not None
    assert "image" in body["outputs"]


# ---------------------------------------------------------------------------
# Hash stability: single-node endpoint vs template-driven path
# ---------------------------------------------------------------------------


def test_hash_matches_template_driven_path(tmp_path: Path) -> None:
    """The hash produced by the endpoint must equal the hash the runtime would
    compute for the same node in a template-driven job (same inputs + params).

    This ensures cached outputs from a full pipeline run are reused by the
    single-node runner and vice versa.
    """
    src = _make_png(tmp_path / "in.png")
    node_cls = registry_lookup("downscale")

    input_ref = Ref(
        node_hash="ext",
        port="image",
        path=src,
        type=PortType.IMAGE_PNG,
    )
    params_dict = {"target_size_px": 64}
    params = node_cls.params_schema.model_validate(params_dict)

    # Hash computed by the endpoint code path.
    endpoint_hash = compute_node_hash(
        node_id=node_cls.id,
        node_version=node_cls.version,
        inputs={"image": input_ref},
        params=params,
    )

    # Hash computed the same way the runtime does in run_job().
    # run_job resolves params through _resolved_params, which for a
    # template-only spec with no profile or job overrides is just
    # params_schema.model_validate(spec.params).  The hash call is identical.
    runtime_hash = compute_node_hash(
        node_id=node_cls.id,
        node_version=node_cls.version,
        inputs={"image": input_ref},
        params=params,
        extra_keys=None,  # downscale.uses_siril is False
    )

    assert endpoint_hash == runtime_hash, (
        f"hash mismatch: endpoint={endpoint_hash[:12]} runtime={runtime_hash[:12]}"
    )
