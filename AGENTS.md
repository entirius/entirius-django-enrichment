# AGENTS.md

`entirius-django-enrichment` (Django app `django_enrichment`).

Horizontal content-quality bus of Entirius — an accept-before-write enrichment loop for product
content (descriptions, attributes, SEO, media). The CMS spawns typed tasks, a worker — external (n8n) or
the internal AI runner — generates proposals, an operator reviews them, and accepted proposals are written back to the
source module (PIM first) through a per-module adapter.

## Commands

| Command | Meaning |
|---|---|
| `make install` | sync dependencies (uv, incl. extras) |
| `make check` | lint + format-check (ruff) |
| `make fix` | auto-fix lint + format |
| `make test` | test suite (pytest + pytest-django) |

## Conventions

- English only: code, docs, commits, branches, PRs.
- MPL-2.0: every non-trivial source file carries the license header (pre-commit inserts it).
- Toolchain: uv + ruff + hatchling + pytest; all config in `pyproject.toml`; `uv.lock` committed.
- Git flow: `master` (production) + `develop` (integration); changes land via PR; semver tag on `master`.
- Never rename the package / Django app_label / DB table prefix `django_enrichment` — it is a schema contract.
- Migrations are part of the public contract — never edit an already released migration.
- Access: areas live on the AppConfig (`access_areas`, `access_route_rules`), every admin view carries
  `access_area`; a new admin route without one fails `tests/test_access_ownership.py`.
- Default: do not commit — git is the user's call.
- No AI attribution in commits or PR descriptions.

## Architecture

```
src/django_enrichment/
├── models/             # EnrichmentTask, ContentProposal, ProposalRevision, SpawnRule, Worker
├── enums.py            # TaskStatus, ProposalStatus, WorkerMode + *_STATUS_TRANSITIONS tables
├── kinds/              # Kind contract + registry (register_kind), base kinds translate / field / video
├── schemas/
│   ├── requests.py     # Pydantic request schemas (with the API)
│   └── responses.py    # Pydantic response schemas (with the API)
├── services/           # task_service, proposal_service, apply_service, undo_service, staging_store,
│                       # cleanup_service, spawn_rule_service, csv_import, worker_service, runner_service,
│                       # toolbox_calls (the only AI toolbox door), revision_service, revise_service
│                       # (+ revise_prompt, revise_values), rules_service, revise_batch_service
├── adapters/           # registry (get_adapter, lazy/DIP) + base.py (Protocol)
├── tasks/              # apply_tasks (canvas), cleanup + gap_autospawn (beats), worker_runner (run_task), revise_tasks
├── api/admin/          # JWT + IsAdminUser ViewSets: views/ (task, proposal, revision, spawn_rule, worker), urls, throttling, pagination
├── admin/              # EnrichmentTaskAdmin, ContentProposalAdmin, SpawnRuleAdmin, WorkerAdmin
├── management/commands/# prune_enrichment (retention), seed_demo_proposals
├── migrations/
├── apps.py             # EnrichmentConfig (name="django_enrichment", is_volkanos=True)
├── settings.py         # ENRICHMENT_* getattr defaults
└── urls.py             # api/enrichment/v2/admin/ → api.admin.urls
```

**Layer rules:** models (ORM only) → schemas (Pydantic, no Django imports) → services (business
logic) → api (thin DRF layer).

**The bus vs the adapter:** the bus (this module) is module-agnostic — it owns tasks, proposals,
the state machine, review/intake, batch apply. It knows nothing about PIM or ContentDB. Per-module
read/write logic lives in an *adapter* registered under a key (`"pim"`, `"contentdb"`) in
`ENRICHMENT_ADAPTERS`. The PIM adapter lives in `django-pim`, not here. `apply_service` routes by
`proposal.target_module` to the registered adapter — never `if target_module == "pim"`.

**Adapter registry (DIP):** `adapters/registry.get_adapter(target_module)` reads
`settings.ENRICHMENT_ADAPTERS` on **every call** (a constant snapshotted at import time would
diverge once the PIM adapter self-registers), lazy-imports the dotted module
path, and caches by path. An adapter is a *module* exposing five duck-typed callables
(`adapters/base.EnrichmentAdapter` Protocol): `resolve_targets`, `find_gaps`, `read_current`,
`apply`, `revert`. No entry → `ValueError` (a client without the module simply has no entry). The
bus never imports a source module — dependency points one way: bus → contract, never bus → PIM.

**Optional adapter hooks:** two callables are looked up with `getattr`, so an adapter may simply not
have them — `release_undo_anchor` (cleanup GC) and `on_reject`. `on_reject(proposal)` fires
after a proposal is rejected (single **and** bulk) for the modules that implement it; the atlas
duplicate adapter uses it to remember "these two are NOT the same product" durably, because the
bus's own reject cooldown expires. Best-effort: the rejection is already committed, so a failing
hook is logged and swallowed. `bulk_reject` stays a single UPDATE for adapters without the hook —
it only re-reads the rows when some module in the batch implements it.

**Service contracts:** see `docs/services.md` for the full per-service contract, the `scope_spec`
shape, and the `filter ⇄ gap` decision.

**Writing a new adapter:** the canonical recipe is `docs/adapter-guide.md` — the contract, registration, and the PIM adapter as the worked reference. The
cross-module pattern behind it (quality flags, `GapDefinition`, candidate → proposal mapping) is
described in the Entirius documentation, https://docs.entirius.com/.

**Media staging (`target_kind=picture`):** a media proposal's binary lives in the bus's own
`staging_store` (filesystem behind `save`/`open_file`/`delete`/`exists`), never the target module,
until accept. Intake is multipart (`POST proposals/upload-media/`, size + magic-byte MIME caps);
reject and apply both drop the staged file; the displaced source object (the old PIM `Picture`) is
the undo anchor. The adapter reads the bytes via `proposal.open_staged_file()` so a source module
never imports `staging_store` (decision D1). Full flow: `docs/media.md`.

**AI workers (internal runner):** a `Worker` row (Django admin only) configures the executor of one
task type — `key == task.type`, a `kind`, `mode` (`internal` | `external`), `system_prompt`, `model`
(blank = the toolbox channel's first catalogue model), `options` (validated by the kind's `Options` in
`clean()`; the admin's `save_model` goes through `worker_service.save_worker` → `full_clean`). `version`
bumps when `system_prompt`/`model`/`options` change and is stamped into every proposal's `source`
(`worker:<key>@v<version>`). A kind (`kinds/`) is code: `Options`, `Params` (validates `task.params`),
`run(worker, task, subject_ref) -> list[ProposalDraft]`; it never writes proposals — the runner hands
drafts to `proposal_service.intake` (`intake_media` when a draft carries `media_file`). Base kinds
`translate` / `field` / `video` register in `EnrichmentConfig.ready()`;
other modules register theirs from their own `ready()`. `task_service.spawn` validates `params` against
the active Worker's kind and, for an active internal one, queues `enrichment.run_task` on commit.
Adapter extras `build_context` (field kind) and `main_picture` (video kind; absent → subject fails) are
optional (`getattr`). Worker keys allow dots (`pim.translate`).

**Revisions & corrections:** `proposed_value` always equals the latest `ProposalRevision`; only
`revision_service.add_revision` writes both (intake writes revision 1). A correction is one stateless
completion (`revise_service`); the answer schema depends on the value shape (`revise_values`). Full
flow: `docs/api.md` § Revisions and corrections.

Tests: `tests/settings.py` is a standalone Django config (sqlite in-memory); `tests/fake_adapter.py` is the
module-as-adapter that lets the whole bus flow run without a source module.

| Topic | Where |
|---|---|
| Overview: entities, why a separate module, review in the CMS | `docs/index.md` |
| Models, state machine, `SpawnRule`, indexes | `docs/data-model.md`, `docs/erd.md`, `docs/erd-config.yaml` |
| Endpoint table, worker pull-loop, idempotency, filters, drift, errors | `docs/api.md` |
| Per-service contract, `scope_spec` shape, the `filter ⇄ gap` decision | `docs/services.md` |
| Writing an adapter | `docs/adapter-guide.md` |
| Media staging flow | `docs/media.md` |
| Async apply, undo, retention | `docs/scale-and-undo.md` |
| Spawn rules, spawn surfaces (CSV, gap, list) | `docs/spawn-rules.md`, `docs/spawn-surfaces.md` |
| Management commands, Celery beat | `docs/commands.md` |
| Test run, suites, the fake adapter | `docs/testing.md` |
| Sibling packages | `README.md` § Requirements |


## Settings

Read in `src/django_enrichment/settings.py`; all optional except `ENRICHMENT_ADAPTERS` for a working bus.

| Setting | Default | Purpose |
|---|---|---|
| `ENRICHMENT_ADAPTERS` | `{}` | adapter key → dotted module path (`{"pim": "django_pim.services.enrichment_adapter"}`) |
| `ENRICHMENT_MAX_IMAGE_BYTES` | 25 MB | media intake size cap |
| `ENRICHMENT_ALLOWED_IMAGE_MIME` | jpeg, png, webp, gif, avif | media intake MIME allowlist |
| `ENRICHMENT_MAX_VIDEO_BYTES` | 100 MB | video intake size cap (mp4 only) |
| `ENRICHMENT_VIDEO_TIMEOUT_S` | `900` | how long the `video` kind polls a toolbox media job |
| `ENRICHMENT_VIDEO_TOOLBOX_URL` / `_API_KEY` / `_CHANNEL` | the `AI_TOOLBOX_*` value | toolbox media job endpoint (POC) |
| `ENRICHMENT_ASYNC_APPLY_THRESHOLD` | `50` | bulk accept/undo above this size goes to Celery |
| `ENRICHMENT_APPLY_BATCH_SIZE` | `500` | batch size of the async apply |
| `ENRICHMENT_CELERY_QUEUE` | `"enrichment"` | queue of the bulk apply / undo canvas |
| `ENRICHMENT_RETENTION_DAYS` | `30` | undo window = retention |
| `ENRICHMENT_MAX_CSV_BYTES` | 10 MB | CSV import size cap |
| `ENRICHMENT_SPAWN_DEFAULT_LIMIT` | `200` | gap-task budget when a rule sets no `limit` |
| `ENRICHMENT_SPAWN_COOLDOWN_DAYS` | `7` | gap cooldown when a rule sets no `cooldown_days` |

## Gotchas

- The PIM adapter does NOT live here — it lives in `django-pim` (`services/enrichment_adapter.py`),
  duck-typed, self-registering. The bus never imports a source module directly.
- `proposed_value` / `current_snapshot` are opaque to the bus — only the adapter's writer reads
  them. Don't add per-module columns (PIM `feature_idx`, etc.) to `ContentProposal`; they go in
  `target_locator`.
- `EnrichmentTask` stays coarse: `open → done`, no lease/retry/DAG. The moment it grows toward a
  durable job queue, that's `django_workflows` — a separate module, not this one.
- **`scope_spec.module` is required** — `EnrichmentTask` has no `target_module` column, so
  `task_service.resolve_targets` reads the adapter key from `scope_spec["module"]`. The request schema carries it.
- **`mode: "gap"` ⇄ `find_gaps`** — `resolve_targets` routes `gap` to `adapter.find_gaps` and
  `filter`/`list` to `adapter.resolve_targets`. `filter` is a simplified `gap` (no named
  check).
- **`mode: "csv"` is parsed by the BUS, not the adapter.** The CSV lives in the bus's own
  `staging_store`, which a source module must never import (decision D1), and `sku,field,type` is
  generic. So `resolve_targets` streams the staged CSV itself (`services/csv_import.stream_targets`)
  and never hands the ref to `adapter.resolve_targets`. One upload stages one file shared by N tasks
  (grouped by the `type` work-type); each task filters to its `scope_spec.row_type`. Full surface
  map: `docs/spawn-surfaces.md`.
- **`bulk_accept`/`bulk_undo` route sync→async by size; `bulk_reject` is always sync.** At/below
  `ENRICHMENT_ASYNC_APPLY_THRESHOLD` (default 50) the apply/undo core runs in-request (counts inline,
  `mode: "sync"`); above it `dispatch_canvas` fans out (`mode: "async"`, `enqueued`). Both touch only
  `pending` (accept) / `applied` (undo) — drifted needs the conscious per-item re-confirm (D2). The
  cores (`apply_many`/`revert_many`) stay sync and directly testable, and stream with `.iterator()`
  (no full-queryset materialisation — OOM, R3). Reject is a single `.update()`. Tests force the Celery
  app eager (`conftest._celery_eager`). Batch-intake is still future.
- **Single-level undo (`undo_service`).** `applied → reverted` only (terminal); deeper history is
  `django-history`. Undo is drift-blocked: `apply_service` stores the post-write live value in
  `applied_snapshot`, and `undo_service.revert` refuses (`UndoBlockedError`) if the live value no
  longer matches it — never clobbers a post-apply edit. `applied_snapshot` stores the adapter's own
  `read_current` result, so the check is generic across text and picture (no per-module branching).
  Undo expires with retention — once the retention beat GC-s the old PIM `Picture`, the way back is gone.
- **Mass apply/undo runs on the `enrichment` Celery queue.** `dispatch_canvas` sets `queue=` per
  signature from `ENRICHMENT_CELERY_QUEUE` (default `enrichment`); the host must run a worker that
  consumes it (`celery -A main worker -Q ...,enrichment`). Re-running a bulk is idempotent — already
  applied → status guard → `failed` bucket, no double-write. Full design: `docs/scale-and-undo.md`.
- **`intake` snapshots via `adapter.read_current`**, never the worker's "before" — drift needs
  ground truth. A new proposal supersedes older `pending` ones on the same
  `(target_module, subject_ref, target_kind, locator_hash)`; `external_ref` makes intake idempotent.
- **Gap-mode resolve is a work queue.** `resolve_targets` for `mode:"gap"` routes through
  `resolve_gap_page`: candidates with a `pending`/`drifted` proposal (or `rejected`/`applied` inside
  `cooldown_days`) drop off, and `params.limit` caps the budget (`counts["proposed"] >= limit` →
  empty). The worker re-pulls **page=1 until empty** — `page++` silently skips items. Dedup key is
  the coarse generic `(target_module, subject_ref, target_kind)`, NOT `locator_hash` — don't "fix"
  it; the bus never reads the opaque locator. Non-dict candidates pass through untouched.
- **`SpawnRule.check_key`, not `check`** — `check` shadows `Model.check()` (models.E020), same trap
  GapDefinition hit. The scope_spec key stays `"check"` (cross-module contract, a dict key).
- **The PIM adapter ignores `SpawnRule.params`** — PIM's rule parameters live on the
  `GapDefinition` row keyed by `check_key`; `params` is forwarded per the contract but reserved.
- **The internal runner uses the default Celery queue** (`run_task.delay`), not `enrichment`; AI calls block it.
- **Runner paging:** gap tasks re-pull page 1 (work queue), others page++; a seen-set stops the loop
  when a page brings no new subject (a failed gap subject never drops off the queue). A target is a SKU
  string or a dict with `subject_ref`. Per-subject failure → `counts.failed`, logged with an error id;
  every subject failed (or resolve itself failed) → task `failed`. No retry, no budget logic. The reason lands in
  `task.errors` as a stable code (`toolbox_calls.failure_code`) + error id — never `str(exc)`.
- **`translate` posts via `ToolboxClient._post`/`_url`** (`retry=False`) — the client has no translator
  method. Tests: `mock_toolbox` plus a respx route for `ai-translator/.../translate/`.
- **Video toolbox endpoint is a POC escape hatch** — `ENRICHMENT_VIDEO_TOOLBOX_*` let media jobs hit a
  local toolbox while text uses the shared test toolbox; remove once ai-toolbox-test has django-ai-media
  (Redmine #34464). Video calls use plain httpx (not `ToolboxClient`, which is pinned to `AI_TOOLBOX_*`).
- **The `video` kind blocks a Celery worker** — it polls the media job (10 s interval) up to
  `ENRICHMENT_VIDEO_TIMEOUT_S` inside `run_task`. Tests patch `toolbox_calls.time.sleep`.
- **`tasks/?subject_ref=` sees list-mode tasks only** — a text match of `"<ref>"` in `scope_spec.refs`
  (SQLite-safe, no JSON containment); filter/gap/csv tasks carry no refs. Full note: `docs/api.md` § Per-product view.
- **Never write `proposed_value` directly** — go through `revision_service.add_revision`, or the
  revision history drifts from the value. Template rule promotion returns 400 until templates exist
  (`rules_service._promote_to_template` is the seam).
- **`run_rule` dedups against `open` AND `in_progress`** — the generic `spawn()` coalesces only
  `open`; a claimed gap task can run for hours and the beat must not stack a second one behind it.
