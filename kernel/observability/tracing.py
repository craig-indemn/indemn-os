"""OpenTelemetry setup and span helpers."""

from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from kernel.config import settings

tracer = trace.get_tracer("indemn-os")


def init_tracing():
    """Initialize OTEL tracing. No-op if no exporter endpoint configured."""
    if not settings.otel_exporter_endpoint:
        return
    provider = TracerProvider()
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


@contextmanager
def create_span(name: str, **attributes):
    """Create an OTEL span with the given name and attributes."""
    with tracer.start_as_current_span(name, attributes=attributes) as span:
        yield span
