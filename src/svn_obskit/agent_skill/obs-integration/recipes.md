# Recipes (svn-obskit)

All imports are `from svn_obskit import ...` unless shown. Replace names with the repo's own.

## FastAPI service

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from svn_obskit import Observability
from svn_obskit.middleware.fastapi import ObservabilityMiddleware

obs = Observability("video-service")           # reads OBSKIT_* env

@asynccontextmanager
async def lifespan(app):
    obs.initialize()                            # logging, delivery, heartbeat, tracing, metrics
    yield
    obs.shutdown()

app = FastAPI(lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware, service_name="video-service")
obs.mount_metrics(app)                          # /metrics (needs svn-obskit[metrics])
```

## Worker + job (queue consumer)

```python
obs = Observability("video-service").initialize()
worker = obs.worker("render-worker", worker_type="render", capacity=2)

for msg in consume():                           # your queue loop
    with worker.job(msg.job_id, workflow_run_id=msg.run_id, metadata={"kind": msg.kind}):
        handle(msg)                             # raise => job FAILED + error captured; re-raised
```
Async code: `async with worker.job(...)`. Use the producer's run id as `workflow_run_id` so the job links to its workflow.

## Workflow with steps and retries (one process)

```python
from svn_obskit import WorkflowTracker
tracker = WorkflowTracker()

with tracker.workflow("news-video-pipeline", workflow_id="news-video") as wf:   # wf.id = run id
    with wf.step("script"):
        make_script()
    for attempt in range(1, 4):
        try:
            with wf.step("render", attempt=attempt, max_attempts=3):
                render()
            break
        except Exception:
            if attempt == 3:
                raise
```
Retries are attempts of the same step (`step_id` defaults to the name). Failing with `attempt < max_attempts`
reports `will_retry` (step RETRYING). `svn_obskit.resilience.Retry.run_step` records attempts for you.

## Workflow across processes / queues

Producer:
```python
run = tracker.start_workflow("news-video-pipeline", workflow_id="news-video")
publish({"run": run.to_dict(), "headers": inject_headers(current_context())})
```
(`from svn_obskit.core.correlation import inject_headers, extract_context`; `from svn_obskit import current_context, use_context`.)

Consumer:
```python
ctx = extract_context(msg["headers"], "render-service")
with use_context(**{k: v for k, v in vars(ctx).items() if v}):
    run = tracker.attach(msg["run"]["workflow_id"], msg["run"]["run_id"])
    with run.step("render"):
        render()
```
The service that finishes the workflow calls `tracker.complete_workflow(run)` / `fail_workflow(run, exc)`.
For start/finish in different processes use `start_step` / `complete_step` / `fail_step` with `StepHandle.to_dict()`.

## Calling another service (keeps trace + workflow context)

```python
from svn_obskit.integrations import httpx as traced
with traced.client(base_url=OTHER_URL) as c:
    c.post("/render", json=payload)            # adds traceparent + x-obskit-* headers
```

## Logging

```python
import logging
log = logging.getLogger(__name__)               # after obs.initialize()
log.info("render started", extra={"frames": 240})
log.exception("render failed")                  # stack trace included, secrets redacted
```
Context fields (`trace_id`, `job_id`, `workflow_run_id`, ...) are attached automatically inside job/step/request scopes.

## Custom span

```python
from svn_obskit.tracing.decorators import span

@span("encode-video", codec="h264")             # low-cardinality attributes only
def encode(...): ...
```

## `/health` for OBS probing

OBS probes `GET /health`: return 2xx + JSON `{"status": "ok", "system": {"cpu_percent": ..., "memory_percent": ..., "disk_percent": ...}}`;
return non-2xx or `"status": "down"` when a critical dependency is broken. Register its URL by setting
`metadata.health_url` or `PUT /services/{id}/instances/{instance}/health-url` on OBS.
