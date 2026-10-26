"""OpenTelemetry distributed tracing.

Configures a global ``TracerProvider`` so that a single child question can be
followed end-to-end: ``kid_query`` → ``model_routing`` → sub-agent → RAG /
calendar tool → guardrail → response. ADK itself emits OpenTelemetry spans for
agent invocations, LLM calls and tool calls; our custom spans nest underneath.

When ``BUDDY_ENABLE_CLOUD_TRACE=true`` spans are exported to Google Cloud Trace
via ``opentelemetry-exporter-gcp-trace``; otherwise an in-memory/no-op setup is
used so tests stay hermetic.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config.settings import get_settings

SERVICE_NAME = "study-buddy-agent"
_INITIALIZED = False


def init_tracing() -> TracerProvider | None:
    """Initialise the global tracer provider (idempotent)."""
    global _INITIALIZED
    if _INITIALIZED:
        return None

    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))

    if settings.enable_cloud_trace and settings.project_id:
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.project_id)))
        except Exception:  # noqa: BLE001 - tracing must never break the agent
            pass

    try:
        trace.set_tracer_provider(provider)
    except Exception:  # noqa: BLE001 - provider already set by the platform (e.g. Agent Runtime)
        pass
    _INITIALIZED = True
    return provider


def get_tracer(name: str = SERVICE_NAME) -> trace.Tracer:
    init_tracing()
    return trace.get_tracer(name)


@contextmanager
def traced_span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Start a child span with PII-safe primitive attributes."""
    from app.observability.pii_redactor import get_pii_scrubber

    scrubber = get_pii_scrubber()
    with get_tracer().start_as_current_span(name) as span:
        for key, value in attributes.items():
            if isinstance(value, str):
                value = scrubber.redact_text(value)
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(f"buddy.{key}", value)
        yield span
