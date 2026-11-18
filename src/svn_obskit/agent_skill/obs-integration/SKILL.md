---
name: obs-integration
description: Instrument a service with svn-obskit so it registers with and reports to the OBS platform (register, edit, remove its service/instance/worker records). Use when adding or reviewing observability in a repo that depends on svn-obskit (svn_obskit imports, OBSKIT_* env vars), or when deciding what information to send to OBS and how - services, instances, workers, jobs, workflows/steps, logs, traces, metrics, errors. Teaches how to classify what you find in code and which SDK call reports it.
---

# svn-obskit integration (agent guide)

This repo uses `svn-obskit` (import name `svn_obskit`) to report to the OBS platform. Your job is to
**look at the code, decide what each piece is, and send it the right way** — not to add telemetry
everywhere. Read this once; code recipes are in `recipes.md`.

## What OBS is, and what it is not

OBS keeps **operational state** (service, instance, worker, job, workflow, incident) and **links** to
telemetry. Logs, traces and metrics live in their own backends (Loki, Jaeger, Prometheus); OBS only
points to them using shared ids. So every piece of information has exactly one correct channel.

## Quick connect (do this first, takes five minutes)

Goal: the service shows up HEALTHY on OBS. Add jobs/workflows afterwards (Steps 1-3).

1. **Get two values** from whoever runs OBS: the URL (`OBSKIT_PLATFORM_URL`, e.g. `https://obs.example.com`) and the
   API key (`OBSKIT_API_KEY`). Never commit the key; put the names in `.env.example` and the values in the
   deployment's secret store. If you do not have them, stop and ask; do not invent values.
2. **Preflight (before touching code)** so a bad URL or key is caught now, not as a silent no-op later:
   ```bash
   curl -fsS "$OBSKIT_PLATFORM_URL/health"                                   # {"status":"ok"}  => reachable
   curl -fsS -H "X-API-Key: $OBSKIT_API_KEY" "$OBSKIT_PLATFORM_URL/auth/check"  # {"ok":true}      => key accepted
   ```
   Unreachable: wrong URL or network/firewall. `401`: wrong key. Fix these first.
3. **Set the env** (minimum): `OBSKIT_PLATFORM_URL`, `OBSKIT_API_KEY`, `OBSKIT_ENVIRONMENT`,
   `OBSKIT_INSTANCE_ID` (fixed per process/pod, never a pid or random value).
4. **Init once per process.** Pick the repo's shape from `recipes.md` (FastAPI lifespan, worker `main()`), or at minimum:
   ```python
   from svn_obskit import Observability

   obs = Observability("<service-name>").initialize()  # registers service + instance, starts heartbeat
   ...
   obs.shutdown()  # on exit, so a planned stop is not seen as a crash
   ```
5. **Confirm it connected:** start the service, wait ~15 s, then
   `curl "$OBSKIT_PLATFORM_URL/services"` must list `<service-name>` HEALTHY with your instance id.

If step 5 fails, use this table (the SDK never raises, so a failure is silent: check OBS, not your logs only):

| Symptom | Cause | Fix |
|---|---|---|
| Log line `no platform_url configured` | `OBSKIT_PLATFORM_URL` not set in the *running* process | set it where the process starts (container env, systemd unit), not just a local shell |
| Service never appears, no errors | wrong URL, or the host cannot reach OBS | repeat the preflight `curl` **from the same host/container** the service runs in |
| `401` on `/auth/check` or nothing registers | missing/wrong `OBSKIT_API_KEY` | use the key OBS issued; check for trailing spaces/newlines |
| Instance registers then goes UNHEALTHY / duplicates pile up | no fixed `OBSKIT_INSTANCE_ID` | set it; remove stale instances with `obs.unregister()` / the platform UI |
| Registration rejected (`422`) | `OBSKIT_HEALTH_URL` is not an absolute `http://` or `https://` URL | fix it, or unset it; it must be reachable *from OBS*, so `localhost` is wrong unless OBS runs on the same host |
| Service shows but health probe is DOWN | `/health` not 2xx, redirects, or URL not reachable from OBS | `curl -i` the exact URL from the OBS host: it must answer 2xx directly (no 301/307) with JSON `{"status":"ok"}` |

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
   `OBSKIT_HEALTH_URL` (if the service has a health route the platform can reach; absolute `http://`/`https://` only, else registration is rejected),
   optionally `OBSKIT_OUTBOX_PATH` (durable), `OBSKIT_OTLP_ENDPOINT`, `OBSKIT_METRICS_PORT`.
   Without `OBSKIT_PLATFORM_URL` the SDK buffers but never delivers — it logs a warning, nothing else.
2. **Init once per process** (web app: FastAPI lifespan; worker script: `main()`); `shutdown()` on exit so a planned stop is not seen as a crash.
3. **HTTP services:** add `ObservabilityMiddleware`; mount `/metrics`; keep a `/health` route (OBS probes it).
4. **Workers/jobs:** one `obs.worker(...)` per consumer loop, one `worker.job(...)` per message.
5. **Workflows:** wrap the pipeline in `tracker.workflow(...)`; each stage in `wf.step(...)`; retries via `attempt`/`max_attempts`.
6. **Crossing a boundary** (HTTP, queue, subprocess): contextvars do **not** cross threads/processes. HTTP: use `integrations.httpx` clients and the middleware. Queues: put `inject_headers(current_context())` (or `StepHandle.to_dict()`/`WorkflowRun.to_dict()`) into the message and restore with `extract_context` / `use_context` / `tracker.attach(...)` in the consumer.

## Registration: what happens automatically, and how to edit or remove it

A service never calls a "register" endpoint by hand. Know which call registers what, and use the registry
only to **correct or clean up its own records**.

| Record on OBS | Created by | Identity (fixed; changing it creates a new record) | Edit | Remove |
|---|---|---|---|---|
| Service | first `initialize()` of any instance | `service_name` + `OBSKIT_ENVIRONMENT` | `obs.registry.update_service(version=, metadata=)` | not from the SDK (it affects every instance; an operator does it on the platform) |
| Instance | `initialize()` (re-registers itself if the platform forgets it) | `OBSKIT_INSTANCE_ID` | `obs.registry.update_instance(hostname=, metadata=)`, `obs.registry.set_health_url(url)` | `obs.unregister()` |
| Worker | `obs.worker(name, ...)` | worker `name` within the instance | `obs.registry.update_worker(worker, worker_type=, capacity=, metadata=)` | `obs.registry.remove_worker(worker)` |
| Job | `worker.job(job_id)` | your `job_id` | RUNNING can be re-reported; **a terminal job (COMPLETED/FAILED/CANCELLED) is frozen**, later updates are ignored | never (history) |
| Workflow run | `tracker.workflow(...)` / `start_workflow` | `workflow_id` + run id | more events | not from the SDK |

Decide with these rules:
- **Set it at registration when you can.** Version → `OBSKIT_SERVICE_VERSION`; health route → `OBSKIT_HEALTH_URL`
  (absolute `http://` or `https://`, reachable *from the platform*; otherwise OBS rejects the registration with 422); worker type/capacity → arguments of `obs.worker(...)`. Use `update_*` only
  for something that changes while the process runs (capacity scaled, a new health URL, a metadata flag).
- **Renaming is not an edit.** A new `service_name`, environment, instance id or worker name registers a *new*
  record and leaves the old one behind. If a rename is intended, remove the old record (`unregister()` /
  `remove_worker`) in the same change.
- **Stop vs. remove.** A normal stop or restart → `obs.shutdown()`: the record stays as STOPPED (OBS deletes it after 7 days of silence; jobs and incidents are kept). Use `obs.unregister()` only when the instance is retired for good (decommissioned pod, a test
  or one-off run), and `remove_worker` only when that worker will not come back.
- **Only your own records.** The platform key is shared, so nothing technical stops a service deleting another's
  data: never call the platform's delete routes for a record this process did not create. Test code that registers
  throwaway services/instances must remove them when it finishes.
- `metadata` is merged; a key set to `None` is removed. Keep it small and free of secrets and PII.
- Every `obs.registry.*` call and `unregister()` returns `True`/`False` and never raises; log or ignore a `False`,
  do not retry in a loop. `obs.registry` is `None` when no `OBSKIT_PLATFORM_URL` is set.

## Known behaviors and limits (verified in the SDK source and the OBS API — do not assume otherwise)

- **A finished job is frozen on OBS.** After `COMPLETED`/`FAILED`/`CANCELLED`, further updates for that `job_id` change nothing. A retry of the work therefore needs a **new `job_id`** (e.g. `f"{msg_id}-try2"`), or should be a step attempt inside a workflow; never reuse the id and expect OBS to show RUNNING again.
- **A rejected event is dropped silently.** `POST /events/batch` answers HTTP 200 even when an event is `REJECTED` (malformed or missing required context); the SDK treats 200 as delivered and never resends. After wiring workflows, check `GET $URL/events/rejected` is empty for your service (and `/monitoring/overview` shows 0 rejected).
- **Record lifecycle on OBS:** an instance/worker with no heartbeat for 30 s becomes UNHEALTHY and opens an incident; after 24 h silent it is marked STOPPED, and after 7 days it is deleted (jobs and incidents are kept). So a crashed pod cleans itself up; `unregister()` is only for removing it sooner.

- **Instance id:** if `OBSKIT_INSTANCE_ID` is unset the SDK uses `name-host-pid-random`, so every restart registers a *new* instance and leaves a duplicate. Always set it.
- **Jobs report `RUNNING` then `COMPLETED`/`FAILED`/`CANCELLED`; the SDK never sends `QUEUED`.** If the service needs queue-wait visibility, POST `/jobs` with `status: QUEUED` yourself at enqueue time (fail-soft, short timeout).
- **Job updates carry `trace_id`** (the active trace, or a fresh one if none), plus `workflow_run_key` when you pass `workflow_run_id=run.id`. That is what lets OBS link Job → Workflow → Trace → Logs. (Versions before this fix did not send it; if `pip show svn-obskit` is older, upgrade.)
- Job updates ride the heartbeat thread in a bounded buffer (1000, oldest dropped); workflow events use the memory buffer or SQLite outbox (at-least-once; platform dedupes). Delivery never raises into business code.
- Logs go to **stderr** as one JSON line each, with `service_name`, `instance_id` and the active context (`trace_id`, `span_id`, `correlation_id`, `workflow_id`, `workflow_run_id`, `step_id`, `job_id`, `worker_id`) as top-level fields. Keys matching password/secret/token/api key are redacted. Extra fields passed via `logger.info("msg", extra={...})` are included.
- Metric labels are bounded by design (method, route template, status class, workflow name, step name, worker type, exception class).
- Not available yet: RabbitMQ transport, transactional outbox in the app DB.

## What OBS does with what you send

- **Incidents open by themselves** when an instance/worker misses heartbeats, a workflow ends `FAILED`/`TIMED_OUT`, or the health probe fails 3 times in a row; they close when the condition clears (`GET $URL/incidents?status=OPEN`). A step failure with `will_retry` does not fail the workflow. Do not report a terminal workflow/job state until it is really final.
- **Health probe contract** (OBS calls `OBSKIT_HEALTH_URL` every 30 s, timeout 5 s): 2xx is UP; a body `status` of `down|error|fail|failed|unhealthy` is DOWN. **Redirects are not followed**, so a route that redirects (e.g. `/health/` vs `/health`, http to https) counts as failed: register the final URL. Body is read up to 64 KB. Optional numbers, top level or under `"system"`, feed the Infra view: `cpu_percent`, `memory_percent`, `disk_percent`, `load_1m`, `cpu_count`, `memory_total_mb`, `memory_used_mb`, `disk_total_gb`, `disk_used_gb`, `process_cpu_percent`, `process_memory_mb`, `active_requests`, `queue_depth`.
- **Links to logs, traces, metrics** (`GET /jobs/{id}/observability`, `/services/{id}/observability`, ...) are built from templates the OBS operator configures. A link is `available: false` when OBS has no template or a placeholder has no value. Make sure your logs carry the ids those links use (`trace_id`, `job_id`, `worker_id`, `workflow_run_id`; they do after `initialize()`), and tell the OBS operator if a link you expect is unavailable; it is an OBS setting, not an SDK bug.

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
5. `GET $URL/events/rejected` has nothing from this service, and `GET $URL/jobs/<obs-job-uuid>/observability` shows a trace link when a trace id exists.
6. Point `OBSKIT_PLATFORM_URL` at a dead host: the service still starts and serves normally.

Finish with a table: concept → found in (file) → reported with → verified how, plus anything skipped and why.
