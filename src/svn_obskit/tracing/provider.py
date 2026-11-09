"""OpenTelemetry tracing. Everything here degrades to a no-op when the libraries are not installed
or no endpoint is configured, so services without OTel behave exactly as before."""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from svn_obskit.core.context import current_context, use_context
from svn_obskit.core.identity import ServiceIdentity

log = logging.getLogger("svn_obskit.tracing")

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace
    from opentelemetry.propagators.textmap import DefaultGetter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, Status, StatusCode, TraceFlags
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the optional extra
    OTEL_AVAILABLE = False

SPAN_KINDS = {"internal", "server", "client", "producer", "consumer"}


class Tracing:
    """Owns a TracerProvider for this service and creates spans that follow the SDK's context."""

    def __init__(self, provider: "TracerProvider", owns_provider: bool = True) -> None:
        self._provider = provider
        self._tracer = provider.get_tracer("svn_obskit")
        self._owns = owns_provider
        self._propagator = TraceContextTextMapPropagator()

    @classmethod
    def configure(
        cls,
        identity: ServiceIdentity,
        *,
        endpoint: str | None = None,
        sample_ratio: float = 1.0,
        processor: "SpanProcessor | None" = None,
        exporter: "SpanExporter | None" = None,
    ) -> "Tracing | None":
        """Build tracing, or return None when OTel is missing or nothing would receive spans.

        `processor`/`exporter` let tests (or another backend) receive spans without an endpoint.
        """
        if not OTEL_AVAILABLE:
            if endpoint:
                log.warning("OBSKIT_OTLP_ENDPOINT is set but opentelemetry is not installed; "
                            "install svn-obskit[otel]")  # fmt: skip
            return None
        if not (endpoint or processor or exporter):
            return None
        resource = Resource.create(
            {
                "service.name": identity.service_name,
                "service.version": identity.service_version,
                "deployment.environment": identity.environment,
                "service.instance.id": identity.instance_id,
                "host.name": identity.hostname,
                "process.pid": identity.process_id,
            }
        )
        provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(sample_ratio)))
        if processor is not None:
            provider.add_span_processor(processor)
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            url = endpoint.rstrip("/") + "/v1/traces"
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url, timeout=5)))
        return cls(provider)

    def shutdown(self) -> None:
        if self._owns:
            try:
                self._provider.shutdown()
            except Exception:
                log.debug("tracer shutdown failed", exc_info=True)

    # --- spans ---------------------------------------------------------------------------

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "internal",
        attributes: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator["trace.Span"]:
        """Start a span as the current span and mirror its ids into the SDK's execution context.

        The parent is, in order: an incoming `traceparent` in `headers`, else the current OTel span,
        else the trace/span ids carried by the SDK context (set from headers or a workflow).
        """
        parent = self._parent_context(headers)
        otel_kind = getattr(SpanKind, kind.upper())
        with self._tracer.start_as_current_span(
            name, context=parent, kind=otel_kind, attributes=dict(attributes or {}), record_exception=False,
            set_status_on_exception=False,
        ) as span:  # fmt: skip
            ctx = span.get_span_context()
            with use_context(trace_id=f"{ctx.trace_id:032x}", span_id=f"{ctx.span_id:016x}"):
                try:
                    yield span
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"[:200]))
                    raise

    def start_client_span(
        self, name: str, attributes: Mapping[str, Any]
    ) -> tuple["trace.Span", dict[str, str]]:
        """A CLIENT span for an outgoing call (not made current) and the headers that continue it."""
        span = self._tracer.start_span(
            name, context=self._parent_context(None), kind=SpanKind.CLIENT, attributes=dict(attributes)
        )
        carrier: dict[str, str] = {}
        self._propagator.inject(carrier, context=trace.set_span_in_context(span))
        return span, carrier

    def _parent_context(self, headers: Mapping[str, str] | None):
        if headers:
            lowered = {k.lower(): v for k, v in headers.items()}
            if "traceparent" in lowered:
                return self._propagator.extract(lowered, getter=DefaultGetter())
        if trace.get_current_span().get_span_context().is_valid:
            return otel_context.get_current()
        ctx = current_context()
        if ctx.trace_id and ctx.span_id:
            remote = SpanContext(
                trace_id=int(ctx.trace_id, 16),
                span_id=int(ctx.span_id, 16),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            return trace.set_span_in_context(NonRecordingSpan(remote))
        return otel_context.get_current()


def end_client_span(
    span: "trace.Span", *, status_code: int | None = None, error: BaseException | None = None
):
    if status_code is not None:
        span.set_attribute("http.response.status_code", status_code)
        if status_code >= 500:
            span.set_status(Status(StatusCode.ERROR, f"HTTP {status_code}"))
    if error is not None:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, type(error).__name__))
    span.end()
