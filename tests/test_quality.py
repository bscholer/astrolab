"""Unit tests for server/quality.py.

Covers:
- compute_quality_for_record returns a dict with all expected keys
- sharpness.laplacian_variance is > 0 for a non-flat image
- compute_quality_for_record returns None (not raises) for a record with no
  image-typed output
- worker _terminate path writes quality_json to the DB
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from astropy.io import fits as astropy_fits

import nodes.basic  # noqa: F401  registers downscale
from server.cache import ContentCache
from server.catalog.db import connect as open_catalog_db
from server.jobs import JobManager
from server.models import Job, NodeSpec, Ref, Template
from server.ports import PortType
from server.quality import compute_quality_for_record
from tests._jobs_helpers import background_worker

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_fits_nonflat(path: Path, shape: tuple[int, int] = (64, 64)) -> Path:
    """Write a 3-channel FITS with a smooth gradient so Laplacian variance > 0."""
    h, w = shape
    ramp = np.linspace(0.0, 1.0, w, dtype=np.float32)
    channel = np.tile(ramp, (h, 1))
    # Slight per-channel offset so color_balance sees R != G != B.
    data = np.stack([channel * 0.8, channel * 0.5, channel * 0.3], axis=0)  # (3, H, W)
    hdu = astropy_fits.PrimaryHDU(data=data)
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu.writeto(str(path), overwrite=True)
    return path


def _make_fits_flat(path: Path) -> Path:
    """Constant-value FITS whose Laplacian variance is 0."""
    data = np.full((1, 32, 32), 0.5, dtype=np.float32)
    astropy_fits.PrimaryHDU(data=data).writeto(str(path), overwrite=True)
    return path


def _fake_job_record_with_fits(fits_path: Path, tmp_path: Path):
    """Build a minimal JobRecord whose primary output points at `fits_path`."""
    db_path = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")
    mgr = JobManager(cache=cache, db_path=db_path)

    template = Template(
        id="test_quality_unit",
        version=1,
        nodes=[],
        outputs={"image": "fake.image"},
    )
    job = Job(template_id="test_quality_unit", template_version=1)
    job_id = mgr.submit(template, job)

    outputs_blob = json.dumps({
        "image": {
            "node_hash": "fakehash",
            "port": "image",
            "path": str(fits_path),
            "type": str(PortType.IMAGE_FITS),
        }
    })
    conn = open_catalog_db(db_path)
    with conn:
        conn.execute(
            "UPDATE jobs SET status='completed', outputs_json=?, finished_at=? WHERE id=?",
            (outputs_blob, "2025-01-01T00:00:00+00:00", job_id),
        )
    conn.close()

    return mgr.get(job_id)


def _fake_job_record_non_image(tmp_path: Path):
    """Build a JobRecord whose only output is a sequence (not an image)."""
    db_path = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")
    mgr = JobManager(cache=cache, db_path=db_path)

    template = Template(
        id="test_quality_seq",
        version=1,
        nodes=[],
        outputs={"seq": "fake.sequence"},
    )
    job = Job(template_id="test_quality_seq", template_version=1)
    job_id = mgr.submit(template, job)

    outputs_blob = json.dumps({
        "seq": {
            "node_hash": "fakehash",
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

    return mgr.get(job_id)


# ---------------------------------------------------------------------------
# Tests: compute_quality_for_record
# ---------------------------------------------------------------------------


def test_quality_dict_shape(tmp_path: Path) -> None:
    """compute_quality_for_record returns all required keys for a valid image."""
    fits_path = _make_fits_nonflat(tmp_path / "output.fits")
    record = _fake_job_record_with_fits(fits_path, tmp_path)
    assert record is not None

    # Stub findstar so we don't need Siril in CI.
    with patch("server.quality._run_findstar", return_value=None):
        result = compute_quality_for_record(record, events=[])

    assert result is not None

    # Top-level keys
    for key in ("output_ref", "dimensions", "channels", "background",
                "color_balance", "siril_warnings", "sharpness"):
        assert key in result, f"missing top-level key {key!r}"

    # dimensions
    dims = result["dimensions"]
    for k in ("width", "height", "channels", "dtype"):
        assert k in dims

    # channels
    assert len(result["channels"]) == 3
    for ch in result["channels"]:
        for k in ("name", "mean", "median", "stdev", "p01", "p50", "p99",
                  "clipped_low_pct", "clipped_high_pct"):
            assert k in ch

    # background must include sigma
    bg = result["background"]
    for k in ("estimated_level", "pct_below_threshold", "sigma"):
        assert k in bg, f"missing background key {k!r}"

    # sharpness
    sh = result["sharpness"]
    for k in ("laplacian_variance", "fwhm_px", "roundness", "star_count"):
        assert k in sh, f"missing sharpness key {k!r}"

    # With findstar stubbed out, FWHM fields are None.
    assert sh["fwhm_px"] is None
    assert sh["roundness"] is None
    assert sh["star_count"] is None


def test_laplacian_variance_positive_for_gradient(tmp_path: Path) -> None:
    """A non-flat gradient image must have laplacian_variance > 0."""
    fits_path = _make_fits_nonflat(tmp_path / "gradient.fits")
    record = _fake_job_record_with_fits(fits_path, tmp_path)
    assert record is not None

    with patch("server.quality._run_findstar", return_value=None):
        result = compute_quality_for_record(record, events=[])

    assert result is not None
    assert result["sharpness"]["laplacian_variance"] > 0.0


def test_returns_none_for_no_image_output(tmp_path: Path) -> None:
    """compute_quality_for_record must return None (not raise) for a
    non-image output (e.g. a FITS sequence)."""
    record = _fake_job_record_non_image(tmp_path)
    assert record is not None

    with patch("server.quality._run_findstar", return_value=None):
        result = compute_quality_for_record(record, events=[])

    assert result is None


def test_returns_none_for_record_with_no_outputs(tmp_path: Path) -> None:
    """compute_quality_for_record must return None when outputs is None."""
    from server.jobs import JobRecord

    template = Template(id="t", version=1, nodes=[], outputs={})
    job = Job(template_id="t", template_version=1)
    record = JobRecord(
        id="fake-id",
        status="completed",
        template=template,
        job=job,
        submitted_at="2025-01-01T00:00:00+00:00",
        outputs=None,
    )
    result = compute_quality_for_record(record, events=[])
    assert result is None


# ---------------------------------------------------------------------------
# Tests: worker _terminate persists quality_json
# ---------------------------------------------------------------------------


def _png(path: Path) -> Path:
    from PIL import Image
    # Gradient so the Laplacian variance is non-zero after downscale.
    arr = np.zeros((64, 64, 3), dtype=np.uint8)
    arr[:, :, 0] = np.tile(np.arange(64, dtype=np.uint8), (64, 1))
    arr[:, :, 1] = np.tile(np.arange(64, dtype=np.uint8).reshape(64, 1), (1, 64))
    arr[:, :, 2] = 60
    Image.fromarray(arr, mode="RGB").save(path)
    return path


def _downscale_template_and_job(src_png: Path) -> tuple[Template, Job]:
    template = Template(
        id="quality_persist_test",
        version=1,
        description="quality persist smoke",
        nodes=[NodeSpec(id="ds", kind="downscale", params={"target_size_px": 32})],
        outputs={"thumb": "ds.image"},
    )
    job = Job(
        template_id="quality_persist_test",
        template_version=1,
        inputs={
            "ds.image": Ref(node_hash="ext", port="image", path=src_png,
                            type=PortType.IMAGE_PNG),
        },
    )
    return template, job


def _wait(mgr: JobManager, jid: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = mgr.get(jid)
        if rec and rec.status in ("completed", "failed"):
            return
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish within {timeout}s")


def test_worker_terminate_writes_quality_json(tmp_path: Path) -> None:
    """After a successful job, quality_json must be populated in the DB."""
    db = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")
    mgr = JobManager(cache=cache, db_path=db)
    template, job = _downscale_template_and_job(_png(tmp_path / "in.png"))

    # Stub findstar so we don't need Siril.
    with (
        patch("server.quality._run_findstar", return_value=None),
        background_worker(cache, db),
    ):
        jid = mgr.submit(template, job)
        _wait(mgr, jid)

    rec = mgr.get(jid)
    assert rec is not None
    assert rec.status == "completed"
    # quality should be on the record
    assert rec.quality is not None
    assert "sharpness" in rec.quality
    assert "background" in rec.quality
    assert "sigma" in rec.quality["background"]

    # Also verify the raw DB column was written.
    conn_raw = sqlite3.connect(db)
    conn_raw.row_factory = sqlite3.Row
    row = conn_raw.execute("SELECT quality_json FROM jobs WHERE id=?", (jid,)).fetchone()
    conn_raw.close()
    assert row is not None
    blob = row["quality_json"]
    assert blob is not None
    parsed = json.loads(blob)
    # Key structure is enough; the exact value depends on the image content.
    assert "sharpness" in parsed
    assert "laplacian_variance" in parsed["sharpness"]
    assert "sigma" in parsed["background"]


def test_quality_in_public_dict(tmp_path: Path) -> None:
    """public_dict must include 'quality' when the record has it."""
    db = tmp_path / "catalog.sqlite"
    cache = ContentCache(root=tmp_path / "cache")
    mgr = JobManager(cache=cache, db_path=db)
    template, job = _downscale_template_and_job(_png(tmp_path / "in.png"))

    with (
        patch("server.quality._run_findstar", return_value=None),
        background_worker(cache, db),
    ):
        jid = mgr.submit(template, job)
        _wait(mgr, jid)

    rec = mgr.get(jid)
    assert rec is not None
    d = rec.public_dict()
    assert "quality" in d
    assert "sharpness" in d["quality"]
    assert "laplacian_variance" in d["quality"]["sharpness"]
