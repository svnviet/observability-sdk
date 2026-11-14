---
name: obs-integration
description: Instrument a service with svn-obskit so it reports to the OBS platform. Use when adding or reviewing observability in a repo that depends on svn-obskit (svn_obskit imports, OBSKIT_* env vars), or when deciding what information to send to OBS and how - services, instances, workers, jobs, workflows/steps, logs, traces, metrics, errors. Teaches how to classify what you find in code and which SDK call reports it.
---

# svn-obskit integration (agent guide)

This repo uses `svn-obskit` (import name `svn_obskit`) to report to the OBS platform. Your job is to
**look at the code, decide what each piece is, and send it the right way** — not to add telemetry
everywhere. Read this once; code recipes are in `recipes.md`.

## What OBS is, and what it is not

OBS keeps **operational state** (service, instance, worker, job, workflow, incident) and **links** to
telemetry. Logs, traces and metrics live in their own backends (Loki, Jaeger, Prometheus); OBS only
points to them using shared ids. So every piece of information has exactly one correct channel.

## Step 1 — Classify what the code does

Walk the repo and fill this table before editing anything. "None found" is a valid answer.

| If the code has… | It is a… | Report with |
|---|---|---|
| A deployable process (web app, consumer, scheduler, CLI daemon) | **Service** (each running process = instance) | `Observability("<name>").initialize()` once at startup; `shutdown()` on exit |
| A loop/thread/consumer that pulls and runs work; a concurrency slot | **Worker** | `obs.worker(name, worker_type=..., capacity=...)` |
| One unit of work with one start and one outcome (queue message, task, request to process) | **Job** | `with worker.job(job_id, workflow_run_id=...)` |
| An ordered multi-step business process, possibly across jobs/services, where "which step failed?" matters | **Workflow** (+ steps, attempts) | `tracker.workflow(...)` / `wf.step(...)`; cross-process: `start_workflow` / `start_step` / `complete_step` / `fail_step` |
| A retry of a step | **Attempt** of that step | `wf.step(name, attempt=n, max_attempts=m)` — never a new step |
| A single call with no stages | just a **Job**, not a workflow | do not invent workflows |

Disambiguation rules:
- Job ≠ step. A step is a stage of a workflow; a job is one execution. A step may run as one job, several, or inline (then no job).
- `workflow_id` is the *kind/identity*, `run.id` is *one execution*. Always keep both; logs and traces are separated by run id.
- A workflow spanning services has **one run id but several traces**. Propagate the run id, not a single trace.
- A job's own id (`external_job_id`) is yours; OBS assigns its own UUID. Always log and send **your** id.

## Step 2 — Classify each piece of information (the right channel)

| Information | Channel | How | Do not |
|---|---|---|---|
| State/lifecycle: is it up, which worker, job running/failed, step retried | **OBS events** | the scopes above (automatic) | write status to logs and hope OBS finds it |
| Why something happened (messages, decisions, errors with context) | **Logs** | plain `logging` after `initialize()` (JSON + context added automatically) | `print`; string-format ids into messages; log secrets |
| Where time went, who called whom | **Traces** | `@span(...)`, `integrations.httpx.client()`, middleware; needs `[otel]` + `OBSKIT_OTLP_ENDPOINT` | create spans per loop iteration |
| Aggregate numbers (rates, durations, queue depth) | **Metrics** | `[metrics]` extra, `obs.mount_metrics(app)` or `OBSKIT_METRICS_PORT` | put ids (job, run, trace, user, article) in labels |
| Small business context useful when debugging (counts, source name, type) | OBS `metadata` / `input_metadata` / `output_metadata` | pass dicts to the SDK calls | send payloads, article bodies, PII, secrets |
| Errors | Raise inside a scope | job/step scopes capture type, message and stack (redacted) | catch-and-swallow inside the scope — OBS then sees success |

Rule of thumb: **ids → logs/traces/OBS, never metric labels; numbers → metrics; state → OBS; text → logs.**
If something fits none of these, it probably does not belong in observability.

## Step 3 — Wire it up (minimum that gives full value)

1. **Config (env, never hard-coded; add to `.env.example`):** `OBSKIT_PLATFORM_URL`, `OBSKIT_API_KEY`,
   `OBSKIT_ENVIRONMENT`, `OBSKIT_SERVICE_VERSION`, **`OBSKIT_INSTANCE_ID` (fixed per process/pod)**,
   optionally `OBSKIT_OUTBOX_PATH` (durable), `OBSKIT_OTLP_ENDPOINT`, `OBSKIT_METRICS_PORT`.
   Without `OBSKIT_PLATFORM_URL` the SDK buffers but never delivers — it logs a warning, nothing else.
2. **Init once per process** (web app: FastAPI lifespan; worker script: `main()`); `shutdown()` on exit so a planned stop is not seen as a crash.
3. **HTTP services:** add `ObservabilityMiddleware`; mount `/metrics`; keep a `/health` route (OBS probes it).
4. **Workers/jobs:** one `obs.worker(...)` per consumer loop, one `worker.job(...)` per message.
5. **Workflows:** wrap the pipeline in `tracker.workflow(...)`; each stage in `wf.step(...)`; retries via `attempt`/`max_attempts`.
6. **Crossing a boundary** (HTTP, queue, subprocess): contextvars do **not** cross threads/processes. HTTP: use `integrations.httpx` clients and the middleware. Queues: put `inject_headers(current_context())` (or `StepHandle.to_dict()`/`WorkflowRun.to_dict()`) into the message and restore with `extract_context` / `use_context` / `tracker.attach(...)` in the consumer.

## Known behaviors and limits (verified in the SDK source — do not assume otherwise)

- **Instance id:** if `OBSKIT_INSTANCE_ID` is unset the SDK uses `name-host-pid-random`, so every restart registers a *new* instance and leaves a duplicate. Always set it.
- **Jobs report `RUNNING` then `COMPLETED`/`FAILED`/`CANCELLED`; the SDK never sends `QUEUED`.** If the service needs queue-wait visibility, POST `/jobs` with `status: QUEUED` yourself at enqueue time (fail-soft, short timeout).
- **Job updates carry `trace_id`** (the active trace, or a fresh one if none), plus `workflow_run_key` when you pass `workflow_run_id=run.id`. That is what lets OBS link Job → Workflow → Trace → Logs. (Versions before this fix did not send it; if `pip show svn-obskit` is older, upgrade.)
- Job updates ride the heartbeat thread in a bounded buffer (1000, oldest dropped); workflow events use the memory buffer or SQLite outbox (at-least-once; platform dedupes). Delivery never raises into business code.
- Logs go to **stderr** as one JSON line each, with `service_name`, `instance_id` and the active context (`trace_id`, `span_id`, `correlation_id`, `workflow_id`, `workflow_run_id`, `step_id`, `job_id`, `worker_id`) as top-level fields. Keys matching password/secret/token/api key are redacted. Extra fields passed via `logger.info("msg", extra={...})` are included.
- Metric labels are bounded by design (method, route template, status class, workflow name, step name, worker type, exception class).
- Not available yet: RabbitMQ transport, transactional outbox in the app DB.

## Never

- Never block or wrap business logic in try/except just for OBS; the SDK already never raises.
- Never create a second logging setup next to `initialize()`; reuse `logging.getLogger(__name__)`.
- Never put secrets or PII in metadata, log fields or error text.
- Never reuse one workflow `run_id` for different executions, or create a new `workflow_id` per run.
- Never mark success manually after catching an exception; let the scope see it.

## Verify (run, do not assume)

1. Start the service with `OBSKIT_PLATFORM_URL`/`OBSKIT_API_KEY` set; `GET $URL/services` lists it HEALTHY with the fixed instance id.
2. Trigger one job → `GET $URL/jobs?external_job_id=<id>` shows RUNNING→COMPLETED with the worker; fail one on purpose → FAILED with the error.
3. Trigger one workflow → `GET $URL/workflow-runs?workflow_id=<id>` shows steps and attempts; force a retry → step RETRYING, then attempt 2.
4. One log line from inside a job parses as JSON and has `job_id`, and `workflow_run_id` when in a workflow.
5. Point `OBSKIT_PLATFORM_URL` at a dead host: the service still starts and serves normally.

Finish with a table: concept → found in (file) → reported with → verified how, plus anything skipped and why.
