"""`/metrics` exposition."""

from __future__ import annotations

from fastapi.testclient import TestClient

from server.api import app
from server.metrics import JOB_STATUSES, _sample


def test_metrics_reports_every_job_status_on_an_empty_database() -> None:
    # A status with no rows must still be exported as 0, or rate()/absent() alerts misfire.
    resp = TestClient(app).get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    for status in JOB_STATUSES:
        assert f'astrolab_jobs{{status="{status}"}} 0' in resp.text
    assert 'astrolab_catalog_rows{table="frames"} 0' in resp.text
    assert "astrolab_cache_budget_bytes " in resp.text


def test_sample_escapes_label_values() -> None:
    # A cache path with a quote or newline would otherwise break the whole scrape.
    line = _sample("m", 1, path='a"b\\c\nd')
    assert line == 'm{path="a\\"b\\\\c\\nd"} 1'
