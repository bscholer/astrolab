"""`/metrics` exposition."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from server.api import app
from server.metrics import JOB_STATUSES, _sample


def test_metrics_exports_every_job_status() -> None:
    # A status with no rows must still be exported, or rate()/absent() alerts misfire.
    resp = TestClient(app).get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    for status in JOB_STATUSES:
        assert re.search(rf'^astrolab_jobs{{status="{status}"}} \d+$', resp.text, re.M)
    assert re.search(r'^astrolab_catalog_rows{table="frames"} \d+$', resp.text, re.M)
    assert re.search(r"^astrolab_cache_budget_bytes \d+$", resp.text, re.M)


def test_sample_escapes_label_values() -> None:
    # A cache path with a quote or newline would otherwise break the whole scrape.
    line = _sample("m", 1, path='a"b\\c\nd')
    assert line == 'm{path="a\\"b\\\\c\\nd"} 1'
