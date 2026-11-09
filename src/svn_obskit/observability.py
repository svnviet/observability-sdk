import logging
from typing import Self

from svn_obskit import runtime
from svn_obskit.core.config import ObservabilityConfig
from svn_obskit.core.identity import ServiceIdentity
from svn_obskit.logging.logger import configure_logging
from svn_obskit.metrics.collectors import RuntimeCollector
from svn_obskit.metrics.registry import PROMETHEUS_AVAILABLE, Metrics
from svn_obskit.tracing.provider import Tracing
from svn_obskit.transport.background import BackgroundPublisher
from svn_obskit.transport.http import HttpTransport
from svn_obskit.transport.interface import Event, EventBuffer
from svn_obskit.transport.memory import MemoryBuffer
from svn_obskit.transport.outbox import SqliteOutbox
from svn_obskit.worker.heartbeat import HeartbeatService
from svn_obskit.worker.lifecycle import Worker
from svn_obskit.worker.registration import PlatformClient
from svn_obskit.workflow.tracker import WorkflowTracker

log = logging.getLogger("svn_obskit")


class Observability:
    """Wires identity, logging and event delivery for one service process."""

    def __init__(
        self,
        service_name: str,
        config: ObservabilityConfig | None = None,
        *,
        span_processor=None,
        **overrides,
    ) -> None:
        self._span_processor = span_processor  # tests and custom backends: receives finished spans
        self.tracing: Tracing | None = None
        self.metrics: Metrics | None = None
        self.config = config or ObservabilityConfig.from_env(service_name, **overrides)
        self.identity = ServiceIdentity.from_config(self.config)
        self._buffer: EventBuffer | None = None
        self._publisher: BackgroundPublisher | None = None
        self._transport: HttpTransport | None = None
        self._heartbeat: HeartbeatService | None = None
        self._platform: PlatformClient | None = None
        self._workers: list[Worker] = []

    def initialize(self) -> Self:
        cfg = self.config
        configure_logging(self.identity, cfg.log_level)
        self._buffer = SqliteOutbox(cfg.outbox_path) if cfg.outbox_path else MemoryBuffer(cfg.buffer_size)
        if cfg.platform_url:
            self._transport = HttpTransport(cfg.platform_url, cfg.api_key, cfg.delivery_timeout_s)
        else:
            log.warning("no platform_url configured: workflow events are buffered but never delivered")
        self._publisher = BackgroundPublisher(
            self._buffer, self._transport, cfg.batch_size, cfg.flush_interval_s, cfg.max_backoff_s
        )
        self._publisher.start()
        if cfg.platform_url:
            self._platform = PlatformClient(cfg.platform_url, cfg.api_key, cfg.delivery_timeout_s)
            self._heartbeat = HeartbeatService(self._platform, self.identity, cfg.heartbeat_interval_s)
            self._heartbeat.start()
        self.tracing = Tracing.configure(
            self.identity,
            endpoint=cfg.otlp_endpoint,
            sample_ratio=cfg.trace_sample_ratio,
            processor=self._span_processor,
        )
        if cfg.metrics_enabled and PROMETHEUS_AVAILABLE:
            self.metrics = Metrics(self.identity)
            self.metrics.add_collector(
                RuntimeCollector(
                    lambda: list(self._workers), lambda: self._heartbeat.age() if self._heartbeat else None
                )
            )
            if cfg.metrics_port:
                self.serve_metrics(cfg.metrics_port)
        runtime.set_active(self)
        return self

    @property
    def tracker(self) -> WorkflowTracker:
        return WorkflowTracker(self)

    def worker(
        self, name: str, *, worker_type: str = "default", capacity: int = 1, metadata: dict | None = None
    ) -> Worker:
        """Create, register and start a worker. Without a platform_url it still tracks state locally."""
        hb = self._heartbeat
        worker = Worker(
            name,
            worker_type,
            capacity,
            metadata,
            on_change=hb.wake if hb else None,
            on_job_update=hb.queue_job_update if hb else None,
        )
        self._workers.append(worker)
        if hb:
            hb.add_worker(worker)
        return worker.start()

    def mount_metrics(self, app, path: str = "/metrics") -> bool:
        """Add a Prometheus scrape route to a FastAPI/Starlette app.

        Safe to call at app creation: the route reads the metrics at scrape time, so it works once
        `initialize()` has run (and answers 503 before that). Returns False if the extra is missing.
        """
        if not PROMETHEUS_AVAILABLE:
            log.warning("metrics are not available; install svn-obskit[metrics]")
            return False
        from prometheus_client import CONTENT_TYPE_LATEST
        from starlette.responses import Response

        async def scrape(request):
            if self.metrics is None:
                return Response("metrics are not enabled\n", status_code=503)
            return Response(self.metrics.render(), media_type=CONTENT_TYPE_LATEST)

        app.add_route(path, scrape, include_in_schema=False)
        return True

    def serve_metrics(self, port: int) -> None:
        """Serve /metrics on a separate port, for workers and jobs that have no web server."""
        if self.metrics is None:
            return
        from prometheus_client import start_http_server

        start_http_server(port, registry=self.metrics.registry)

    def dead_letter_count(self) -> int:
        return self._buffer.dead_letter_count() if isinstance(self._buffer, SqliteOutbox) else 0

    def emit(self, event: Event) -> None:
        if self._publisher is None:
            raise RuntimeError("Observability.initialize() has not been called")
        self._publisher.submit(event)

    def pending_events(self) -> int:
        return self._publisher.pending() if self._publisher else 0

    def flush(self, timeout_s: float = 5.0) -> bool:
        return self._publisher.flush(timeout_s) if self._publisher else True

    def shutdown(self, timeout_s: float = 5.0) -> None:
        if runtime.get_active() is self:
            runtime.set_active(None)
        for worker in self._workers:
            worker.stop()
        if self._heartbeat:
            self._heartbeat.stop()
        if self._platform:
            self._platform.close()
        if self._publisher:
            self._publisher.shutdown(timeout_s)
        if self._transport:
            self._transport.close()
        if self.tracing:
            self.tracing.shutdown()  # flushes spans still waiting in the exporter
        if isinstance(self._buffer, SqliteOutbox):
            self._buffer.close()

    def __enter__(self) -> Self:
        return self.initialize()

    def __exit__(self, *exc_info) -> None:
        self.shutdown()
