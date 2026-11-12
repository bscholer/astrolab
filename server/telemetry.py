"""Send traces to an OpenTelemetry collector, when one is configured.

Tracing is on only when OTEL_EXPORTER_OTLP_ENDPOINT (or the traces-only variant) is set,
for example http://otel-collector:4318. Spans go out as OTLP over HTTP. Without it, the
tracer is OpenTelemetry's no-op, so spans in the code cost next to nothing.

What gets traced: API requests (minus the health probe, /metrics and static files),
outgoing HTTP calls (the scope relay, weather), and background work that no request
covers: scheduler ticks, observations, scans, compass runs, mirror passes, and pipeline
jobs with a span per node.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

log = logging.getLogger(__name__)

tracer = trace.get_tracer("astrolab")

# Regexes searched in the full request URL. The probe hits the bare root.
EXCLUDED_URLS = r"^https?://[^/]+/$,/metrics,/_app/,/favicon"


def enabled() -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )


def setup(service_name: str) -> bool:
    """Install the tracer provider and HTTP client tracing. Returns whether tracing is on."""
    if not enabled():
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.urllib import URLLibInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        name = os.environ.get("OTEL_SERVICE_NAME", service_name)
        provider = TracerProvider(resource=Resource.create({"service.name": name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        URLLibInstrumentor().instrument()
        log.info("tracing to %s as %s", os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"), name)
        return True
    except Exception:
        log.exception("could not set up tracing, carrying on without it")
        return False


def instrument_app(app: Any) -> None:
    """Trace FastAPI requests. Call once, after setup()."""
    if not enabled():
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)
    except Exception:
        log.exception("could not trace API requests")


def node_spans(events: Callable[[dict], None]) -> Callable[[dict], None]:
    """Wrap a pipeline event sink so each node becomes a child span of the current one."""
    open_spans: dict[str, Any] = {}

    def sink(ev: dict) -> None:
        kind, nid = ev.get("type"), ev.get("node_id")
        if kind == "node_started" and nid:
            span = tracer.start_span(f"node {ev.get('kind') or nid}")
            span.set_attribute("astrolab.node_id", nid)
            if ev.get("kind"):
                span.set_attribute("astrolab.node_kind", ev["kind"])
            open_spans[nid] = span
        elif kind in ("node_cached", "node_completed", "node_failed") and nid in open_spans:
            span = open_spans.pop(nid)
            span.set_attribute("astrolab.cached", kind == "node_cached")
            if kind == "node_failed":
                span.set_status(Status(StatusCode.ERROR, str(ev.get("error") or "failed")))
            span.end()
        events(ev)

    return sink


def traced(name: str) -> Callable:
    """Run the function in a span. An exception marks the span failed and goes on up."""

    def wrap(fn: Callable) -> Callable:
        import functools

        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with tracer.start_as_current_span(name):
                return fn(*args, **kwargs)

        return inner

    return wrap


def note(**attrs: Any) -> None:
    """Add attributes to the current span. Values that are None are left out."""
    span = trace.get_current_span()
    for k, v in attrs.items():
        if v is not None:
            span.set_attribute(f"astrolab.{k}", v)


def fail(error: object) -> None:
    """Mark the current span failed, for code that catches its own errors."""
    trace.get_current_span().set_status(Status(StatusCode.ERROR, str(error)))
