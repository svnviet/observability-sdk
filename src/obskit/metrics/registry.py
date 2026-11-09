"""Prometheus metrics with bounded labels.

Rule: a label value may only come from a small, fixed set (HTTP method, route template, status class,
workflow name, step name, worker type, exception class). Ids (workflow, run, job, article) and error
messages are never labels. `BoundedLabels` enforces a ceiling as a safety net: once a label has seen
`max_values` distinct values, new ones are reported as "other".
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

from obskit.core.identity import ServiceIdentity

log = logging.getLogger("obskit.metrics")

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, make_asgi_app

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the optional extra
    PROMETHEUS_AVAILABLE = False

HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
LONG_BUCKETS = (0.1, 0.5, 1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600)


class BoundedLabels:
    def __init__(self, max_values: int = 100) -> None:
        self._max = max_values
        self._seen: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def clamp(self, label: str, value: str | None) -> str:
        value = value or "unknown"
        with self._lock:
            seen = self._seen.setdefault(label, set())
            if value in seen:
                return value
            if len(seen) >= self._max:
                return "other"
            seen.add(value)
            return value


def status_class(status: int | None) -> str:
    return "error" if status is None else f"{status // 100}xx"


class Metrics:
    """All SDK metrics for one service process, in one registry (scrape it at /metrics)."""

    def __init__(self, identity: ServiceIdentity, max_label_values: int = 100) -> None:
        self.registry = CollectorRegistry()
        self._labels = BoundedLabels(max_label_values)
        r = self.registry
        self._http_requests = Counter(
            "obskit_http_requests_total",
            "HTTP requests served.",
            ["method", "route", "status_class"],
            registry=r,
        )
        self._http_duration = Histogram(
            "obskit_http_request_duration_seconds", "HTTP request latency.", ["method", "route"],
            buckets=HTTP_BUCKETS, registry=r,
        )  # fmt: skip
        self._http_in_progress = Gauge(
            "obskit_http_requests_in_progress", "HTTP requests being served.", registry=r
        )
        self._client_duration = Histogram(
            "obskit_http_client_request_duration_seconds", "Outgoing HTTP call latency.",
            ["host", "method", "status_class"], buckets=HTTP_BUCKETS, registry=r,
        )  # fmt: skip
        self._db_duration = Histogram(
            "obskit_db_operation_duration_seconds", "Database statement duration.", ["operation"],
            buckets=HTTP_BUCKETS, registry=r,
        )  # fmt: skip
        self._exceptions = Counter(
            "obskit_exceptions_total", "Exceptions seen by the SDK.", ["exception_type", "source"], registry=r
        )
        self._wf_started = Counter(
            "obskit_workflows_started_total", "Workflows started.", ["workflow"], registry=r
        )
        self._wf_finished = Counter(
            "obskit_workflows_finished_total", "Workflows finished.", ["workflow", "status"], registry=r
        )
        self._wf_duration = Histogram(
            "obskit_workflow_duration_seconds", "Workflow duration.", ["workflow", "status"],
            buckets=LONG_BUCKETS, registry=r,
        )  # fmt: skip
        self._step_duration = Histogram(
            "obskit_workflow_step_duration_seconds", "Workflow step attempt duration.",
            ["workflow", "step", "status"], buckets=LONG_BUCKETS, registry=r,
        )  # fmt: skip
        self._step_retries = Counter(
            "obskit_workflow_step_retries_total",
            "Step attempts beyond the first.",
            ["workflow", "step"],
            registry=r,
        )
        self._queue_wait = Histogram(
            "obskit_workflow_queue_wait_seconds", "Time a workflow waited before it started.", ["workflow"],
            buckets=LONG_BUCKETS, registry=r,
        )  # fmt: skip
        self._jobs = Counter(
            "obskit_worker_jobs_total", "Jobs processed by workers.", ["worker_type", "result"], registry=r
        )
        self._job_duration = Histogram(
            "obskit_worker_job_duration_seconds", "Worker job duration.", ["worker_type"],
            buckets=LONG_BUCKETS, registry=r,
        )  # fmt: skip
        Gauge(
            "obskit_build_info", "Service identity (always 1).", ["service_name", "service_version", "environment"],
            registry=r,
        ).labels(identity.service_name, identity.service_version, identity.environment).set(1)  # fmt: skip

    # --- recording API (all calls are cheap and never raise into business code) -----------------

    def http_started(self) -> None:
        self._http_in_progress.inc()

    def http_finished(self, method: str, route: str | None, status: int | None, seconds: float) -> None:
        self._http_in_progress.dec()
        method, route = (
            self._labels.clamp("method", method),
            self._labels.clamp("route", route or "unmatched"),
        )
        self._http_requests.labels(method, route, status_class(status)).inc()
        self._http_duration.labels(method, route).observe(seconds)

    def client_call(self, host: str | None, method: str, status: int | None, seconds: float) -> None:
        self._client_duration.labels(
            self._labels.clamp("host", host), self._labels.clamp("method", method), status_class(status)
        ).observe(seconds)

    def db_operation(self, operation: str, seconds: float) -> None:
        self._db_duration.labels(self._labels.clamp("db_operation", operation.upper())).observe(seconds)

    def exception(self, exc: BaseException | type[BaseException], source: str) -> None:
        name = exc.__name__ if isinstance(exc, type) else type(exc).__name__
        self._exceptions.labels(self._labels.clamp("exception_type", name), source).inc()

    def workflow_started(self, workflow: str | None) -> None:
        self._wf_started.labels(self._labels.clamp("workflow", workflow)).inc()

    def workflow_finished(self, workflow: str | None, status: str, seconds: float | None) -> None:
        name = self._labels.clamp("workflow", workflow)
        self._wf_finished.labels(name, status).inc()
        if seconds is not None:
            self._wf_duration.labels(name, status).observe(max(0.0, seconds))

    def workflow_queue_wait(self, workflow: str | None, seconds: float) -> None:
        self._queue_wait.labels(self._labels.clamp("workflow", workflow)).observe(max(0.0, seconds))

    def step_finished(self, workflow: str | None, step: str, status: str, seconds: float | None) -> None:
        if seconds is not None:
            self._step_duration.labels(
                self._labels.clamp("workflow", workflow), self._labels.clamp("step", step), status
            ).observe(max(0.0, seconds))

    def step_retry(self, workflow: str | None, step: str) -> None:
        self._step_retries.labels(
            self._labels.clamp("workflow", workflow), self._labels.clamp("step", step)
        ).inc()

    def job_finished(self, worker_type: str, result: str, seconds: float) -> None:
        wt = self._labels.clamp("worker_type", worker_type)
        self._jobs.labels(wt, result).inc()
        self._job_duration.labels(wt).observe(max(0.0, seconds))

    # --- exposition ------------------------------------------------------------------------

    def add_collector(self, collector: Any) -> None:
        self.registry.register(collector)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def asgi_app(self) -> Callable:
        """An ASGI app serving the Prometheus text format. Mount it at /metrics."""
        return make_asgi_app(registry=self.registry)
