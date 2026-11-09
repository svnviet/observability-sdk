import pytest

from obskit import runtime
from obskit.core.config import ObservabilityConfig
from obskit.core.identity import ServiceIdentity


class FakeSink:
    def __init__(self) -> None:
        self.identity = ServiceIdentity.from_config(ObservabilityConfig(service_name="svc-a"))
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e["event_type"] for e in self.events]


@pytest.fixture
def sink():
    s = FakeSink()
    runtime.set_active(s)
    yield s
    runtime.set_active(None)


@pytest.fixture
def obs():
    """A real Observability with in-memory span capture and metrics, no platform."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from obskit import Observability

    exporter = InMemorySpanExporter()
    o = Observability(
        "svc-a", span_processor=SimpleSpanProcessor(exporter), environment="test", heartbeat_interval_s=3600
    ).initialize()
    o.exporter = exporter
    o.events = lambda: [e for _, e in o._buffer.peek(1000)]
    yield o
    o.shutdown()
