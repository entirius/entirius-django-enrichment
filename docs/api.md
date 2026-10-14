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
| `GET` | `workers/?target_module=` | `worker_service.list_active` | Active workers, plain list `[{key, label, kind, target_module, mode, params_schema}]` |
| `POST` | `tasks/` | `task_service.spawn` | Spawn a work order. `scope_spec.module` required. `params` validated by the kind when an active Worker owns `type` (400 otherwise). Throttled (`enrichment_spawn`, 120/min). → 201 |
| `POST` | `tasks/import-csv/` | `task_service.import_csv` | Multipart CSV (`sku,field,type`) → N tasks grouped by `type`. One file = one channel + one language. Streamed lazily from one staged file. Throttled. → 201 |
| `GET` | `tasks/` | `task_service.list_tasks` | Filter `type`/`status` (one or a comma list)/`batch_key`/`target_module`/`subject_ref`, paginated |
| `GET` | `tasks/{id}/` | `task_service.get_task` | Retrieve |
| `GET` | `tasks/{id}/targets/?page=` | `task_service.resolve_targets` | Lazy worker pull (adapter-resolved) |
| `PATCH` | `tasks/{id}/` | `task_service.transition_status` | Drive the state machine (e.g. `in_progress`, `done`) |
| `GET/POST` | `spawn-rules/` | `spawn_rule_service.list_rules`/`create_rule` | SpawnRule CRUD. Filters: `active`, `search`. → 200/201 |
| `GET/PATCH/DELETE` | `spawn-rules/{key}/` | `spawn_rule_service` | `key` immutable (schema drops it) |
| `POST` | `spawn-rules/{key}/run/` | `spawn_rule_service.run_rule` | Throttled (`enrichment_spawn`). → 201 `spawned` / 200 `already_running` / 200 `no_candidates` |
| `POST` | `proposals/` | `proposal_service.intake` | Worker intake — idempotent on `external_ref`. Throttled (`enrichment_intake`, 100/min). → 201 |
| `POST` | `proposals/upload-media/` | `proposal_service.intake_media` | Multipart media intake — stages the binary, `target_kind=picture`. Size + MIME caps. Throttled. → 201 |
| `GET` | `proposals/{id}/staged-file/` | `staging_store` | Stream a proposal's staged binary (admin-only) — the CMS "after" preview |
| `GET` | `proposals/` | `proposal_service.list_for_review` | Filters: status/module/kind/batch/source/confidence_min/search/subject_ref/`task` (id); `ordering` allowlisted |
| `GET` | `proposals/{id}/` | `proposal_service.get_proposal` | Retrieve |
| `PATCH` | `proposals/{id}/` | `proposal_service.update_value` | Body `{proposed_value}` — pending/drifted only, else 400. Creates a `user` revision |
| `GET` | `proposals/{id}/revisions/` | `revision_service.list_revisions` | Plain list `[{number, value, instruction, scope, author_kind, author, created_at}]` |
| `POST` | `proposals/{id}/revise/` | `revise_service.revise` | `{instruction?, quick_action?, scope?}` → 201 revision, 200 `{candidates}` for `variants`; 400 / 422 `REVISION_NOT_APPLICABLE`; toolbox errors mapped. Throttled (`enrichment_bulk`) |
| `POST` | `proposals/{id}/revisions/{n}/restore/` | `revision_service.restore` | → 201 `restore` revision copying `n` (404 unknown `n`) |
| `PUT` | `proposals/{id}/rules/` | `rules_service.set_rules` | `{rules: [str]}` ≤ 20 × ≤ 200 chars → 200 proposal |
| `POST` | `proposals/{id}/rules/promote/` | `rules_service.promote` | `{rule, target: template\|worker}` → 200 `{target, rules}`; template → 400 until templates exist |
| `POST` | `tasks/{id}/revise/` | `revise_batch_service.queue` | `{instruction?, quick_action?}` (no `variants`) → 202 `{queued}`; Celery `enrichment.revise_task`. Throttled |
| `POST` | `proposals/{id}/accept/` | `proposal_service.accept` | Drift-aware apply via the adapter |
| `POST` | `proposals/{id}/reject/` | `proposal_service.reject` | Body `{reason}` |
| `POST` | `proposals/bulk-accept/` | `proposal_service.bulk_accept` | Pending-only. Sync in-request `≤ ENRICHMENT_ASYNC_APPLY_THRESHOLD`, else Celery canvas. → 200 `{mode, enqueued, applied, drifted, failed}` |
| `POST` | `proposals/bulk-reject/` | `proposal_service.bulk_reject` | Sync `.update()`. → `{rejected}` |
| `POST` | `proposals/bulk-undo/` | `proposal_service.bulk_undo` | Single-level undo of the filtered APPLIED set; same sync/canvas routing. Drift-blocked items stay applied. → 200 `{mode, enqueued, reverted, blocked, failed}` |

## Revisions and corrections (contract A3 § 1)

Every proposal carries a revision history (`ProposalRevision`, 1-based `number`); `proposed_value`
always equals the latest revision — only `revision_service` writes both, in one transaction. Intake
records revision 1 (`ai`); `PATCH` records `user`; restore records `restore`; corrections record `ai`.
Only `pending`/`drifted` proposals take new revisions; media proposals cannot be corrected.

A correction is **one stateless completion**. Prompt: worker system prompt (worker from
`source = worker:<key>@v<n>`) as the system message; then rules (proposal → template → worker
`options.rules`), the task's `params.prompt`, the field catalog entry (adapter `describe_fields`,
optional), the current value, the selection, the instruction (quick actions `shorten` / `expand` /
`formal` / `casual` / `fit_limit` / `variants` expand server-side). Model = the worker's (blank → default).

| Value | Answer schema | Applied as |
|---|---|---|
| `{"text"}` with HTML or > 300 chars | `{edits: [{old, new}], rewrite}` | `rewrite` → full value; else each `old` must occur exactly once (else 422, nothing saved); HTML sanitised |
| `{"text"}` short | `{text}` (+ catalog `maxLength`) | full value |
| `{"option"}` / `{"options"}` | catalog option enum | full value |

Scope `{"type": "selection", "text"}` must occur exactly once in the current text (else 400).
Batch: each pending text proposal of the task gets one call; failures land in `task.errors`
(`REVISION_NOT_APPLICABLE` or the toolbox code), counts `revised` / `failed`.

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
`batch_id`, `source`, `confidence_min`, `search` (matches `subject_label`), `subject_ref` (exact). `ordering` is validated
against an allowlist (`created_at`, `confidence`, `status`, `subject_ref`, each `±`) before reaching
`order_by` — an unknown field is a 400, never an ORM error that leaks column names.

## Per-product view

The CMS product page shows the AI work of one product with two reads:

```
GET /proposals/?subject_ref=SKU-1&target_module=pim&status=pending
GET /tasks/?subject_ref=SKU-1&target_module=pim&page_size=5        # recent tasks, any status
```

`tasks/?subject_ref=` matches **list-mode tasks only** (`scope_spec.refs`); `filter`/`gap`/`csv` tasks carry
no refs and never match. It is a text match of the quoted ref inside the `refs` array (whole element,
case-insensitive), not JSON containment — no index; fine while the task table grows with commands, not the
catalogue. `target_module` matches `scope_spec.module`.

Every task response carries `errors` (why subjects failed — stable codes, see `docs/data-model.md`) and
`proposal_counts` (`{status: n}` of the task's proposals, one grouped query per page), so the CMS can say
"1 proposal to review", "applied" or "failed: translator not configured" without more calls.

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
