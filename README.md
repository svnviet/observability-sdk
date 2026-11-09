# obskit (SDK)

Lightweight instrumentation for Services. Only runtime dependency: `httpx`.

```python
from obskit import Observability, WorkflowTracker

obs = Observability("video-generator").initialize()  # reads OBSKIT_* env vars
tracker = WorkflowTracker()

with tracker.workflow("news-video-pipeline", workflow_id="wf-123") as wf:
    with wf.step("video-render", attempt=1, max_attempts=3):  # async: `async with`
        render_video()
```

- Cross-service: add `ObservabilityMiddleware` to FastAPI; call other services with
  `integrations.httpx.client()/async_client()`; downstream code uses `tracker.current().step(...)`.
- Cross-process: `tracker.start_workflow / start_step / complete_step / fail_step`; `StepHandle.to_dict()`.
- Delivery never blocks or raises into business code. Events go to a bounded memory buffer, or to a SQLite
  outbox when `OBSKIT_OUTBOX_PATH` is set (survives crashes; at-least-once, the platform deduplicates).

| Variable | Meaning |
|---|---|
| `OBSKIT_PLATFORM_URL`, `OBSKIT_API_KEY` | where and how to deliver events |
| `OBSKIT_OUTBOX_PATH` | enables the durable outbox |
| `OBSKIT_ENVIRONMENT`, `OBSKIT_SERVICE_VERSION`, `OBSKIT_INSTANCE_ID` | identity |
| `OBSKIT_FLUSH_INTERVAL_S`, `OBSKIT_MAX_BACKOFF_S`, `OBSKIT_LOG_LEVEL` | tuning |

## Spans and metrics (optional extras)

```bash
pip install "obskit[otel,metrics]"
```

- **Tracing** (`[otel]`, `OBSKIT_OTLP_ENDPOINT=http://otel-collector:4318`): spans for requests, workflows, steps
  (one per attempt), worker jobs, outgoing calls (through `integrations.httpx`) and SQL (`integrations.sqlalchemy.instrument`).
  The trace id stored on a workflow run is the real trace id. `@span(...)` adds your own.
- **Metrics** (`[metrics]`): call `obs.mount_metrics(app)` for a Prometheus route at `/metrics`, or set
  `OBSKIT_METRICS_PORT` for processes without a web server. Covers process, HTTP, outgoing calls, database,
  exceptions, workers and workflows. Labels are bounded: only method, route template, status class, workflow name,
  step name, worker type and exception class; a label that produces over 100 values folds into `other`.
- Without the extras, or without an endpoint, both switch themselves off and everything else works unchanged.

## Workers, jobs and heartbeats

```python
worker = obs.worker("render-worker", worker_type="render", capacity=2)  # registers and heartbeats
with worker.job(job_id, workflow_run_id=run.id):  # async: `async with`
    render(job_id)
```

A heartbeat thread reports the service instance and every worker on `OBSKIT_HEARTBEAT_INTERVAL_S`,
independent of job execution. `obs.shutdown()` reports a planned stop, so only a crash looks like one.

## Resilience (`obskit.resilience`)

`Retry` / `RetryPolicy` (backoff, jitter, `run_step` records every attempt as a workflow step attempt),
`with_timeout` / `run_with_timeout` / `@timeout`, `CircuitBreaker` (CLOSED, OPEN, HALF_OPEN, state-change
callback), `Bulkhead`, and `IdempotencyGuard` with memory or SQLite stores. Each works in sync and async code.
Nothing here changes workflow state silently: retries show up as attempts.

## Built / not built

Built: OpenTelemetry spans, Prometheus metrics, SQLAlchemy instrumentation, identity, execution context (contextvars), W3C `traceparent` + correlation propagation, JSON logging
with redaction, workflow/step/attempt tracking, workers and heartbeats, resilience tools, transport (memory
buffer, SQLite outbox with dead letters, HTTP, background batching with backoff), FastAPI middleware, httpx
propagation.

Not built yet: RabbitMQ integration and transport, and a transactional outbox inside the application's own database.
