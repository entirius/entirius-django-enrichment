---
title: Admin API
description: v2 admin API for enrichment tasks, proposals, review, and apply.
---

v2 admin API under `/api/enrichment/v2/admin/`. JWT + `IsAdminUser` on every ViewSet (the n8n
worker authenticates with an admin service account — no separate auth tor). Pydantic v2 schemas,
v2 error envelope, standard pagination. Views are thin: parse → service → Response; all ORM lives
in the services.

## Endpoints

Error envelope `{error, message, debug_id, details}`, pagination `count/next/previous/results`.

| Method | Endpoint | Service | Notes |
|--------|----------|---------|-------|
| `POST` | `tasks/` | `task_service.spawn` | Spawn a work order. `scope_spec.module` required. Throttled (`enrichment_spawn`, 120/min). → 201 |
| `POST` | `tasks/import-csv/` | `task_service.import_csv` | Multipart CSV (`sku,field,type`) → N tasks grouped by `type`. One file = one channel + one language. Streamed lazily from one staged file. Throttled. → 201 |
| `GET` | `tasks/` | `task_service.list_tasks` | Filter `type`/`status`/`batch_key`, paginated |
| `GET` | `tasks/{id}/` | `task_service.get_task` | Retrieve |
| `GET` | `tasks/{id}/targets/?page=` | `task_service.resolve_targets` | Lazy worker pull (adapter-resolved) |
| `PATCH` | `tasks/{id}/` | `task_service.transition_status` | Drive the state machine (e.g. `in_progress`, `done`) |
| `GET/POST` | `spawn-rules/` | `spawn_rule_service.list_rules`/`create_rule` | SpawnRule CRUD. Filters: `active`, `search`. → 200/201 |
| `GET/PATCH/DELETE` | `spawn-rules/{key}/` | `spawn_rule_service` | `key` immutable (schema drops it) |
| `POST` | `spawn-rules/{key}/run/` | `spawn_rule_service.run_rule` | Throttled (`enrichment_spawn`). → 201 `spawned` / 200 `already_running` / 200 `no_candidates` |
| `POST` | `proposals/` | `proposal_service.intake` | Worker intake — idempotent on `external_ref`. Throttled (`enrichment_intake`, 100/min). → 201 |
| `POST` | `proposals/upload-media/` | `proposal_service.intake_media` | Multipart media intake — stages the binary, `target_kind=picture`. Size + MIME caps. Throttled. → 201 |
| `GET` | `proposals/{id}/staged-file/` | `staging_store` | Stream a proposal's staged binary (admin-only) — the CMS "after" preview |
| `GET` | `proposals/` | `proposal_service.list_for_review` | Filters: status/module/kind/batch/source/confidence_min/search; `ordering` allowlisted |
| `GET` | `proposals/{id}/` | `proposal_service.get_proposal` | Retrieve |
| `POST` | `proposals/{id}/accept/` | `proposal_service.accept` | Drift-aware apply via the adapter |
| `POST` | `proposals/{id}/reject/` | `proposal_service.reject` | Body `{reason}` |
| `POST` | `proposals/bulk-accept/` | `proposal_service.bulk_accept` | Pending-only. Sync in-request `≤ ENRICHMENT_ASYNC_APPLY_THRESHOLD`, else Celery canvas. → 200 `{mode, enqueued, applied, drifted, failed}` |
| `POST` | `proposals/bulk-reject/` | `proposal_service.bulk_reject` | Sync `.update()`. → `{rejected}` |
| `POST` | `proposals/bulk-undo/` | `proposal_service.bulk_undo` | Single-level undo of the filtered APPLIED set; same sync/canvas routing. Drift-blocked items stay applied. → 200 `{mode, enqueued, reverted, blocked, failed}` |

## The n8n worker pull-loop

The worker owns no selection logic — it pulls typed tasks, generates, and posts proposals back:

```
GET  /tasks/?status=open&type=fix-attribute      # claim work
GET  /tasks/{id}/targets/?page=1                 # resolve scope lazily (adapter-paged)
POST /proposals/   {task_id, external_ref, ...}  # submit one proposal per target/field
PATCH /tasks/{id}/ {"status": "done"}            # close the task (after mark in_progress)
```

`targets` never materialises the whole catalogue — the adapter bounds each page (PIM: SKUs).

**Gap-mode tasks are a work queue:** `scope_spec.mode == "gap"` targets are post-filtered (targets
with a pending/drifted proposal, or rejected/applied inside the cooldown window, drop off) and
capped by `params.limit`. The worker therefore re-pulls **`page=1` until it comes back empty** —
`page++` would silently skip items as consumed targets fall off the front. A failed generation is
submitted as a low-confidence proposal carrying the error note, never silently skipped (the same
target would re-surface forever). `page++` stays correct for `csv`/`list`/`filter` (static sets).

## Spawn rules (etap-13)

Operator config mapping a module quality check to a work-type — see `docs/spawn-surfaces.md`
(§ Gap spawn) for semantics. `key` is the immutable identifier (slug); `check_key` is the
module-side rule key (PIM: `GapDefinition.key`), deliberately unvalidated here — an unknown key
fails as a 400 at run time, raised by the adapter.

```
GET    /spawn-rules/                 # list — filters: active, search; paginated
POST   /spawn-rules/                 # create
GET    /spawn-rules/{key}/           # retrieve
PATCH  /spawn-rules/{key}/           # update (key immutable — unknown fields dropped by the schema)
DELETE /spawn-rules/{key}/           # delete (open tasks are not touched)
POST   /spawn-rules/{key}/run/       # run now (enrichment_spawn throttle)
```

`run` answers: `201 {status:"spawned", task}` (fresh task), `200 {status:"already_running", task}`
(an open/in_progress task with the rule's `gaprule:` batch_key exists), or
`200 {status:"no_candidates", task:null}` (the page-1 probe, after the dedup/cooldown filter,
came back empty). `GET /tasks/?batch_key=` supports the CMS running indicator.

## Intake idempotency

`POST /proposals/` is idempotent on `external_ref` (the worker's execution id): re-posting the same
`external_ref` returns the existing proposal instead of creating a duplicate. Intake also snapshots
the live value via `adapter.read_current` (never the worker's "before") and supersedes any older
`pending` proposal on the same `(target_module, subject_ref, target_kind, locator_hash)` target.

Intake is throttled (`enrichment_intake` scope, `100/min` fallback) — it's the high-volume,
worker-facing path. The other endpoints (operator review) are not throttled.

## Review filters

`GET /proposals/` filters server-side: `status` (default `pending`), `target_module`, `target_kind`,
`batch_id`, `source`, `confidence_min`, `search` (matches `subject_label`). `ordering` is validated
against an allowlist (`created_at`, `confidence`, `status`, `subject_ref`, each `±`) before reaching
`order_by` — an unknown field is a 400, never an ORM error that leaks column names.

## Accept / drift (D2)

`POST /proposals/{id}/accept/` routes through the drift-aware apply: it re-reads the live value and,
if it diverged from the snapshot, flips the proposal to `drifted` (no write) for the operator to
re-confirm. No drift → the adapter writes and the proposal becomes `applied`. `bulk-accept/` does the
same over a filtered `pending` set asynchronously (Celery); `bulk-reject/` is a synchronous
`.update()`.

## Errors

All errors use the v2 envelope from `django-utils`:

```json
{"error": "VALIDATION_ERROR", "message": "...", "debug_id": "f7a2c3b8", "details": []}
```

`VALIDATION_ERROR` (400, Pydantic or service `ValueError`), `NOT_FOUND` (404, unknown id),
`AUTHENTICATION_REQUIRED` (401), `PERMISSION_DENIED` (403, non-admin), `RATE_LIMITED` (429, intake
throttle). Internal errors never leak `str(exc)` — the handler returns `INTERNAL_ERROR` + a logged
`debug_id`.

## OpenAPI

Every action is `@extend_schema`-annotated (tags `Enrichment Tasks` / `Enrichment Proposals`), so the
schema generates without warnings. Browse it at `/api/docs/` (Swagger) or `/api/schema/`.
