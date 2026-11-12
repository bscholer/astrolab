"""Tracing: off without an endpoint, and spans for background work and pipeline nodes."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from server import telemetry

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture(autouse=True)
def _clear():
    EXPORTER.clear()


def test_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert telemetry.setup("astrolab") is False


def test_traced_work_notes_attributes_and_failures():
    @telemetry.traced("scope.observe")
    def run():
        telemetry.note(target="M 31", gain=None)
        telemetry.fail("slewing: refused")

    run()
    (span,) = EXPORTER.get_finished_spans()
    assert span.name == "scope.observe"
    assert span.attributes["astrolab.target"] == "M 31"
    assert "astrolab.gain" not in span.attributes
    assert span.status.status_code == StatusCode.ERROR


def test_pipeline_nodes_become_child_spans_of_the_job():
    seen = []
    sink = telemetry.node_spans(seen.append)
    with telemetry.tracer.start_as_current_span("job"):
        sink({"type": "node_started", "node_id": "a", "kind": "stack"})
        sink({"type": "node_completed", "node_id": "a"})
        sink({"type": "node_started", "node_id": "b", "kind": "stretch"})
        sink({"type": "node_cached", "node_id": "b"})
        sink({"type": "node_started", "node_id": "c", "kind": "graxpert"})
        sink({"type": "node_failed", "node_id": "c", "error": "boom"})
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    job = spans["job"]
    assert {"node stack", "node stretch", "node graxpert"} <= set(spans)
    assert all(spans[n].parent.span_id == job.context.span_id for n in spans if n != "job")
    assert spans["node stretch"].attributes["astrolab.cached"] is True
    assert spans["node graxpert"].status.status_code == StatusCode.ERROR
    assert len(seen) == 6
