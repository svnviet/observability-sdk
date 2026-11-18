---
name: obs-service-profile
description: Read this repo and publish a short profile of the service (what it does, its capabilities, interfaces, dependencies, background work, limits) to the OBS platform, so anyone opening the service's detail page in OBS sees it. Use when asked to describe, document, profile or "register the capabilities of" a service on OBS, after adding or removing a feature, or right after connecting a service with the obs-integration skill.
---

# Publish a service profile to OBS

Goal: the service's page in OBS (Services → click the service) shows an accurate, short description of what
this service **is and can do**, written from the code, not from guesses. It is stored in the service's
`metadata.profile`; `obskit profile publish` validates and sends it.

Prerequisites: `svn-obskit>=0.4.0`, `OBSKIT_PLATFORM_URL` and `OBSKIT_API_KEY` set, and the service has
registered at least once (run it once with `obs.initialize()`; check `curl $OBSKIT_PLATFORM_URL/services`).
If it is not registered yet, publishing fails with a message saying so; do not register a fake instance to get around it.

## Steps

1. **Read the repo before writing anything.** In this order: README/docs, dependency file (language,
   frameworks), entry points (`main`, app factory, CLI), route definitions (HTTP interfaces), queue
   consumers and schedulers (background work), config/env names (dependencies), existing `obs.worker(...)` /
   `tracker.workflow(...)` calls. Open the files; do not infer from names alone.
2. **Write `obs-profile.json`** at the repo root using the schema below. Every line must be backed by
   something you read. If you could not confirm a thing, leave it out (or put it under `limitations` as unknown).
3. **Validate:** `obskit profile publish obs-profile.json --service <service_name> --dry-run`.
   Fix what it reports (missing summary or capabilities, wrong types). It trims over-long text and drops unknown keys.
4. **Publish:** same command without `--dry-run`. `--service` must equal the `service_name` the service registers
   with (the first argument of `Observability("<name>")`), and `OBSKIT_ENVIRONMENT` must match the environment it runs in.
5. **Confirm:** `curl "$OBSKIT_PLATFORM_URL/services"` → take the service `id` → `curl "$OBSKIT_PLATFORM_URL/services/<id>"`
   must show `metadata.profile`. Open `$OBSKIT_PLATFORM_URL/#/services/<id>` to see it rendered.
6. Report what you published and which files you read. Re-run the whole procedure when a feature is added,
   changed or removed; the profile has a `generated_at` so staleness is visible.

## Schema (`obs-profile.json`)

```json
{
  "summary": "One to three sentences: what this service does and for whom.",
  "language": "python",
  "frameworks": ["fastapi", "sqlalchemy"],
  "capabilities": [
    {"name": "Render video", "description": "Turns an approved script and assets into an mp4 using ffmpeg."}
  ],
  "interfaces": [
    {"kind": "http", "name": "POST /videos", "description": "Create a render request; returns a job id."}
  ],
  "dependencies": [
    {"kind": "database", "name": "PostgreSQL", "description": "Stores render requests and results."}
  ],
  "background": [
    {"kind": "worker", "name": "render-worker", "description": "Consumes the render queue, capacity 2."}
  ],
  "limitations": ["Only 1080p output", "No retry after the third failed render"],
  "repo": "https://github.com/org/video-service",
  "commit": "a1b2c3d"
}
```

- `summary` (required, ≤1000 chars) and `capabilities` (required, ≥1, ≤30) are mandatory; everything else is optional.
- `interfaces.kind`: `http|grpc|queue|event|cron|cli|other`. `dependencies.kind`:
  `database|cache|queue|storage|service|api|other`. `background.kind`: `worker|job|workflow|scheduler|other`.
- `name` ≤120 chars, `description` ≤400 chars. The whole profile must stay under 16 KB: this is a summary, not documentation.
- Describe capabilities in **business terms** (what a user or another service gets), then list the technical
  surface under `interfaces`. Prefer fewer, accurate entries over a long guessed list.

## Never put in a profile

- Secrets, tokens, keys, passwords, connection strings, or the contents of `.env` files.
- Internal IPs, private hostnames, customer names, personal data. Dependencies are named by technology
  (`PostgreSQL`), not by address.
- Anything you did not verify in the code. No marketing language; do not claim features that are planned or half-built.
- Another service's description: a profile covers only the service in this repo.

The profile is visible to everyone who can open OBS, and OBS reads need no key. Treat it as public inside your organization.
