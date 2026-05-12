"""Tests for GET /api/jobs/{job_id}/quality.

Covers:
- 404 on unknown job id
- 409 on a queued (not-yet-finished) job
- 400 on a job whose output isn't an image port
- Successful path: synthetic FITS -> full quality response with sane values
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as astropy_fits
from fastapi.testclient import TestClient
from PIL import Image

import nodes.basic  # noqa: F401  registers downscale
from server.api import app, job_manager
from server.cache import ContentCache
from server.catalog.db import connect as open_catalog_db
from server.jobs import JobRecord
from server.models import Job, Ref, Template
from server.ports import PortType
from tests._jobs_helpers import background_worker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    monkeypatch.setattr(job_manager, "_cache", cache)
    job_manager.reset_for_tests(db_path=db_path)
    with background_worker(cache, db_path), TestClient(app) as c:
        yield c


def _make_png(path: Path, size: tuple[int, int] = (64, 32)) -> Path:
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    arr[:, :, 0] = 80   # R
    arr[:, :, 1] = 120  # G
    arr[:, :, 2] = 60   # B
    Image.fromarray(arr, mode="RGB").save(path)
    return path


def _make_fits_rgb(path: Path, shape: tuple[int, int] = (32, 64)) -> Path:
    """Write a 3-channel (C, H, W) float32 FITS whose values are known."""
    h, w = shape
    data = np.zeros((3, h, w), dtype=np.float32)
    data[0] = 0.30   # R channel mean
    data[1] = 0.50   # G channel mean
    data[2] = 0.20   # B channel mean
    hdu = astropy_fits.PrimaryHDU(data=data)
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu.writeto(str(path), overwrite=True)
    return path


def _downscale_payload(src_png: Path) -> dict:
    return {
        "template": {
            "id": "test_downscale",
            "version": 1,
            "description": "smoke",
            "nodes": [{"id": "ds", "kind": "downscale", "params": {"target_size_px": 32}}],
            "outputs": {"thumb": "ds.image"},
        },
        "job": {
            "template_id": "test_downscale",
            "template_version": 1,
            "inputs": {
                "ds.image": {
                    "node_hash": "ext",
                    "port": "image",
                    "path": str(src_png),
                    "type": "image/png",
                }
            },
        },
    }


def _wait_for(client, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_quality_404_on_unknown_job(client) -> None:
    r = client.get("/api/jobs/not-a-real-id/quality")
    assert r.status_code == 404


def test_quality_409_on_queued_job(client, tmp_path: Path, monkeypatch) -> None:
    """Submit a job and patch get() to return it as queued, then call quality."""
    src = _make_png(tmp_path / "in.png")
    job_id = client.post("/api/jobs", json=_downscale_payload(src)).json()["job_id"]

    original_get = job_manager.get

    def _patched_get(jid: str):
        rec = original_get(jid)
        if rec is not None and jid == job_id:
            return JobRecord(
                id=rec.id,
                status="queued",
                template=rec.template,
                job=rec.job,
                submitted_at=rec.submitted_at,
            )
        return rec

    monkeypatch.setattr(job_manager, "get", _patched_get)
    r = client.get(f"/api/jobs/{job_id}/quality")
    assert r.status_code == 409
    assert "not completed" in r.json()["detail"]


def test_quality_success_png(client, tmp_path: Path) -> None:
    """End-to-end happy path: downscale a PNG, check quality response shape."""
    src = _make_png(tmp_path / "in.png", size=(64, 32))
    job_id = client.post("/api/jobs", json=_downscale_payload(src)).json()["job_id"]
    body = _wait_for(client, job_id)
    assert body["status"] == "completed"

    r = client.get(f"/api/jobs/{job_id}/quality")
    assert r.status_code == 200, r.text
    data = r.json()

    # Top-level keys
    for key in ("output_ref", "dimensions", "channels", "background", "siril_warnings"):
        assert key in data

    # output_ref
    assert data["output_ref"]["type"] == "image/png"
    assert Path(data["output_ref"]["path"]).exists()

    # dimensions
    dims = data["dimensions"]
    assert dims["channels"] == 3
    assert dims["width"] > 0
    assert dims["height"] > 0
    assert dims["dtype"] == "float32"

    # channels: RGB named correctly
    assert len(data["channels"]) == 3
    assert data["channels"][0]["name"] == "R"
    assert data["channels"][1]["name"] == "G"
    assert data["channels"][2]["name"] == "B"
    for ch in data["channels"]:
        for key in ("mean", "median", "stdev", "p01", "p50", "p99",
                    "clipped_low_pct", "clipped_high_pct"):
            assert key in ch, f"missing {key!r} in channel"
        assert 0.0 <= ch["mean"] <= 1.0
        assert 0.0 <= ch["p99"] <= 1.0

    # background
    assert "estimated_level" in data["background"]
    assert "pct_below_threshold" in data["background"]
    assert 0.0 <= data["background"]["estimated_level"] <= 1.0

    assert isinstance(data["siril_warnings"], list)


def test_quality_success_fits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a synthetic FITS directly as a job's completed output and assert
    the quality endpoint returns per-channel stats with roughly correct values."""
    fits_path = _make_fits_rgb(tmp_path / "output.fits")

    db_path = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")
    monkeypatch.setattr(job_manager, "_cache", cache)
    job_manager.reset_for_tests(db_path=db_path)

    template = Template(
        id="test_fits_quality",
        version=1,
        nodes=[],
        outputs={"image": "fake.image"},
    )
    job = Job(template_id="test_fits_quality", template_version=1)
    job_id = job_manager.submit(template, job)

    ref = Ref(
        node_hash="testhash",
        port="image",
        path=fits_path,
        type=PortType.IMAGE_FITS,
    )
    outputs_blob = json.dumps({
        "image": {
            "node_hash": ref.node_hash,
            "port": ref.port,
            "path": str(ref.path),
            "type": str(ref.type),
        }
    })
    conn = open_catalog_db(db_path)
    with conn:
        conn.execute(
            "UPDATE jobs SET status='completed', outputs_json=?, finished_at=? WHERE id=?",
            (outputs_blob, "2025-01-01T00:00:00+00:00", job_id),
        )
    conn.close()

    with TestClient(app) as c:
        r = c.get(f"/api/jobs/{job_id}/quality")

    assert r.status_code == 200, r.text
    data = r.json()

    assert data["output_ref"]["type"] == "image/fits"
    dims = data["dimensions"]
    assert dims["channels"] == 3

    # After normalisation to [0,1] across the whole cube, G is highest, B lowest.
    r_mean = data["channels"][0]["mean"]
    g_mean = data["channels"][1]["mean"]
    b_mean = data["channels"][2]["mean"]
    assert g_mean > r_mean > b_mean, (
        f"expected G > R > B but got R={r_mean:.3f} G={g_mean:.3f} B={b_mean:.3f}"
    )

    assert "r_g_ratio" in data["color_balance"]
    assert "b_g_ratio" in data["color_balance"]


def test_quality_400_non_image_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job whose only output is a SEQUENCE_FITS should return 400."""
    db_path = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")
    monkeypatch.setattr(job_manager, "_cache", cache)
    job_manager.reset_for_tests(db_path=db_path)

    template = Template(
        id="test_seq_quality",
        version=1,
        nodes=[],
        outputs={"seq": "fake.sequence"},
    )
    job = Job(template_id="test_seq_quality", template_version=1)
    job_id = job_manager.submit(template, job)

    outputs_blob = json.dumps({
        "seq": {
            "node_hash": "testhash",
            "port": "sequence",
            "path": str(tmp_path / "seq"),
            "type": str(PortType.SEQUENCE_FITS),
        }
    })
    conn = open_catalog_db(db_path)
    with conn:
        conn.execute(
            "UPDATE jobs SET status='completed', outputs_json=?, finished_at=? WHERE id=?",
            (outputs_blob, "2025-01-01T00:00:00+00:00", job_id),
        )
    conn.close()

    with TestClient(app) as c:
        r = c.get(f"/api/jobs/{job_id}/quality")

    assert r.status_code == 400
    assert "image" in r.json()["detail"].lower()
