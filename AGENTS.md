# AGENTS.md

## Quick Reference

Horizontal content-quality bus for Volkanos — an accept-before-write enrichment loop for product
content (descriptions, attributes, SEO, media). The CMS spawns typed tasks, an external worker
(n8n) generates proposals, an operator reviews them, and accepted proposals are written back to the
source module (PIM first) through a per-module adapter.

**Status:** models, service layer, adapter registry, the PIM adapter (in `django-pim`), and the v2
admin API have landed. The full bus flow (spawn → resolve → intake → supersede → drift → apply) runs
against the registered adapter and is exposed over HTTP at `/api/enrichment/v2/admin/`. Next: the
CMS review queue (etap-06) and the n8n worker (etap-07).

**Tech:** Python >=3.11, Django >=5.0, DRF, Pydantic, drf-spectacular, Celery 5+, django-utils (BaseModel)

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
- Default: do not commit — git is the user's call.

## Architecture

```
src/django_enrichment/
├── models/             # EnrichmentTask, ContentProposal, SpawnRule
├── enums.py            # TaskStatus, ProposalStatus + *_STATUS_TRANSITIONS tables
├── schemas/
│   ├── requests.py     # Pydantic request schemas (with the API)
│   └── responses.py    # Pydantic response schemas (with the API)
├── services/           # task_service, proposal_service, apply_service, undo_service, staging_store, cleanup_service, spawn_rule_service
├── adapters/           # registry (get_adapter, lazy/DIP) + base.py (Protocol)
├── tasks/              # apply_tasks (Celery canvas) + cleanup (retention beat) + gap_autospawn (etap-13 beat)
├── api/admin/          # JWT + IsAdminUser ViewSets (empty)
├── admin/              # EnrichmentTaskAdmin, ContentProposalAdmin
├── management/commands/# prune_enrichment (retention)
├── migrations/
├── apps.py             # EnrichmentConfig (name="django_enrichment", is_volkanos=True)
├── settings.py         # ENRICHMENT_ADAPTERS getattr placeholder
└── urls.py             # urlpatterns = []  (routes added with the API)
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
diverge once the PIM adapter self-registers — finding etap-01), lazy-imports the dotted module
path, and caches by path. An adapter is a *module* exposing five duck-typed callables
(`adapters/base.EnrichmentAdapter` Protocol): `resolve_targets`, `find_gaps`, `read_current`,
`apply`, `revert`. No entry → `ValueError` (a client without the module simply has no entry). The
bus never imports a source module — dependency points one way: bus → contract, never bus → PIM.

**Service contracts:** see `docs/services.md` for the full per-service contract, the `scope_spec`
shape, and the `filter ⇄ gap` decision.

**Writing a new adapter:** the canonical recipe is the [Adapter Guide](https://docs.entirius.com/volkanos/modules/enrichment/adapter-guide/)
in entirius-docs — the contract, registration, and the PIM adapter as the worked reference. The
cross-module pattern behind it (quality flags, `GapDefinition`, candidate → proposal mapping) is
[Gaps Per Module](https://docs.entirius.com/architecture/gaps-per-module/).

**Media staging (`target_kind=picture`, etap-08):** a media proposal's binary lives in the bus's own
`staging_store` (filesystem behind `save`/`open_file`/`delete`/`exists`), never the target module,
until accept. Intake is multipart (`POST proposals/upload-media/`, size + magic-byte MIME caps);
reject and apply both drop the staged file; the displaced source object (the old PIM `Picture`) is
the undo anchor. The adapter reads the bytes via `proposal.open_staged_file()` so a source module
never imports `staging_store` (decision D1). Full flow: `docs/media.md`.

## File Map

| File | Purpose |
|------|---------|
| `apps.py` | `EnrichmentConfig` — app registration |
| `settings.py` | `ENRICHMENT_ADAPTERS` registry placeholder (filled when the first adapter lands) |
| `urls.py` | Root URL routing (empty until the API stage) |
| `tests/settings.py` | Standalone Django config (sqlite in-memory) |
| `tests/test_smoke.py` | App loads + package imports (no DB) |
| `docs/erd-config.yaml` | ERD model groupings (empty until models exist) |
| `pyproject.toml` | Package config, dependencies, ruff, pytest |

## Data Model

Three models. `EnrichmentTask` 1 ──* `ContentProposal`; `SpawnRule` is standalone operator
config (no FK to either — it references tasks only through the deterministic `gaprule:` batch_key).
All inherit `BaseModel`
(`created_at`/`modified_at`). Transition logic lives in services (next stage) — the models
themselves carry no business logic. State machines are declared as data in `enums.py`
(`TASK_STATUS_TRANSITIONS`, `PROPOSAL_STATUS_TRANSITIONS`).

| Model | Key fields | Notes |
|-------|-----------|-------|
| `EnrichmentTask` | `type`, `params` (opaque JSON), `scope_spec` (filter/csv/list), `batch_key`, `status`, `requested_by`, `counts` | Thin work order. One row per command — targets resolved lazily, table grows with commands not catalogue. Lifecycle `open → in_progress → done \| cancelled \| failed`. |
| `ContentProposal` | `task`, `target_module`/`target_type`/`subject_ref`/`subject_label`/`subject_url`/`target_kind`/`target_locator`, `locator_hash`, `proposed_value`/`staged_file`/`current_snapshot`, `status`, `source`, `confidence`, `batch_id`, `external_ref`, review/apply audit | Row-per-field result, generic target (decision #11). `locator_hash` = sha1 of canonical `target_locator`, computed in `save()`, indexes the superseded lookup. Lifecycle `pending → applied \| rejected \| superseded \| drifted`; `drifted → applied \| rejected`. |
| `SpawnRule` | `key` (unique slug, immutable), `module`, `check_key` (module-side rule key, string, NO FK), `params`, `scope` (`{channel, language}`), `task_type`, `task_params`, `limit` (null = setting), `cooldown_days` (null = setting), `auto`, `active` | One quality check → one work-type (etap-13). Run by the CMS "Run now" button or the `enrichment.gap_autospawn` beat (`auto=True`). Spawns gap-mode tasks with `batch_key="gaprule:<key>"`. |

**Generic target (decision #11):** no PIM-shaped columns. `target_module` keys the adapter; PIM
specifics (`feature_idx`, `channel`, `language`) live in `target_locator` JSON. `proposed_value` /
`current_snapshot` are opaque — only the target module's adapter writer reads them.

**Indexes (scale 1M):** task `(status, type)` + `batch_key`; proposal `(status, subject_ref)`,
`(target_module, subject_ref, target_kind, locator_hash)`, `(status, applied_at)` + `subject_ref` /
`target_module` / `batch_id` / `external_ref`. No partitioning yet (deferred); `(status, applied_at)`
keeps the cleanup beat and a future partition cheap.


## API Contract

v2 admin API under `/api/enrichment/v2/admin/`. JWT (`rest_framework_simplejwt`) + `IsAdminUser`
(staff or superuser) on every ViewSet — the n8n worker uses an admin service account. Pydantic v2
request/response schemas, v2 error envelope (`{error, message, debug_id, details}`), standard
pagination (`count/next/previous/results`). Thin views — all ORM through the services.

| Method | Endpoint | Service | Notes |
|--------|----------|---------|-------|
| `POST` | `tasks/` | `task_service.spawn` | Spawn a work order. `scope_spec.module` required. Throttled (`enrichment_spawn`, 120/min). → 201 |
| `POST` | `tasks/import-csv/` | `task_service.import_csv` | Multipart CSV (`sku,field,type`) → N tasks grouped by `type` (etap-11). One file = one channel + one language. Streamed lazily from one staged file. Throttled. → 201 |
| `GET` | `tasks/` | `task_service.list_tasks` | Filter `type`/`status`/`batch_key`, paginated |
| `GET` | `tasks/{id}/` | `task_service.get_task` | Retrieve |
| `GET` | `tasks/{id}/targets/?page=` | `task_service.resolve_targets` | Lazy worker pull (adapter-resolved) |
| `PATCH` | `tasks/{id}/` | `task_service.transition_status` | Drive the state machine (e.g. `in_progress`, `done`) |
| `GET/POST` | `spawn-rules/` | `spawn_rule_service.list_rules`/`create_rule` | SpawnRule CRUD (etap-13). Filters: `active`, `search`. → 200/201 |
| `GET/PATCH/DELETE` | `spawn-rules/{key}/` | `spawn_rule_service` | `key` immutable (schema drops it) |
| `POST` | `spawn-rules/{key}/run/` | `spawn_rule_service.run_rule` | Throttled (`enrichment_spawn`). → 201 `spawned` / 200 `already_running` / 200 `no_candidates` |
| `POST` | `proposals/` | `proposal_service.intake` | Worker intake — idempotent on `external_ref`. Throttled (`enrichment_intake`, 100/min). → 201 |
| `POST` | `proposals/upload-media/` | `proposal_service.intake_media` | Multipart media intake (etap-08) — stages the binary, `target_kind=picture`. Size + MIME caps. Throttled. → 201 |
| `GET` | `proposals/{id}/staged-file/` | `staging_store` | Stream a proposal's staged binary (admin-only) — the CMS "after" preview |
| `GET` | `proposals/` | `proposal_service.list_for_review` | Filters: status/module/kind/batch/source/confidence_min/search; `ordering` allowlisted |
| `GET` | `proposals/{id}/` | `proposal_service.get_proposal` | Retrieve |
| `POST` | `proposals/{id}/accept/` | `proposal_service.accept` | Drift-aware apply via the adapter |
| `POST` | `proposals/{id}/reject/` | `proposal_service.reject` | Body `{reason}` |
| `POST` | `proposals/bulk-accept/` | `proposal_service.bulk_accept` | Pending-only. Sync in-request `≤ ENRICHMENT_ASYNC_APPLY_THRESHOLD`, else Celery canvas. → 200 `{mode, enqueued, applied, drifted, failed}` |
| `POST` | `proposals/bulk-reject/` | `proposal_service.bulk_reject` | Sync `.update()`. → `{rejected}` |
| `POST` | `proposals/bulk-undo/` | `proposal_service.bulk_undo` | Single-level undo of the filtered APPLIED set; same sync/canvas routing. Drift-blocked items stay applied. → 200 `{mode, enqueued, reverted, blocked, failed}` |

Worker pull-loop: `GET tasks/?status=open` → `GET tasks/{id}/targets/` → `POST proposals/` →
`PATCH tasks/{id}/` (done). Full contract + idempotency + filters: `docs/api.md`.

## Dependencies

**Runtime:** django, djangorestframework, djangorestframework-simplejwt, pydantic, drf-spectacular,
celery (>=5 — `bulk_accept` dispatches `apply_proposals_task`; the host service registers the task +
queue and wires `config_from_object`).

**Volkanos modules:** `django-utils` (BaseModel — `created_at` + `modified_at`).

**Adapters:** the PIM adapter lives in `django-pim` (`services/enrichment_adapter.py`); the host
service registers it via `ENRICHMENT_ADAPTERS = {"pim": "django_pim.services.enrichment_adapter"}`
and the bus loads it lazily (DIP — no hard import of `django-pim`).

## Testing

```bash
make test                              # pytest (sqlite in-memory, --no-migrations)
make check                             # ruff check + format check
```

`addopts = --no-migrations` (pytest-django builds the schema straight from the models). Suites:
`test_smoke` (DB-less), `test_models`, `test_registry`, `test_task_service`, `test_proposal_service`,
`test_cleanup` (retention prune). The in-memory `tests/fake_adapter.py` (a module-as-adapter) lets the
whole bus flow be tested without `django-pim`; `conftest.fake_adapter` registers it via
`override_settings`. Its `release_undo_anchor` is a spy (`fake_adapter.released()`) so cleanup tests
can assert the bus invokes the GC hook for the right proposals.

## Management Commands

| Command | What it does |
|---------|-------------|
| `python manage.py prune_enrichment` | Retention prune (etap-10): delete terminal proposals + done tasks older than `ENRICHMENT_RETENTION_DAYS`, drop staged files, GC displaced PIM `Picture`s. `--days N` overrides the window (`--days 0` prunes everything qualifying). Same work the daily beat does. |

**Celery beat:** the host schedules `enrichment.prune` daily at 02:00 UTC and
`enrichment.gap_autospawn` every 30 min (etap-13 — runs every `auto=True, active=True` SpawnRule;
per-rule batch_key dedup + page-1 probe make an idle pass one cheap query per rule), both on the
`enrichment` queue
(`CELERY_BEAT_SCHEDULE` in the service settings). One knob `ENRICHMENT_RETENTION_DAYS` (default 30) =
undo window = retention = storage upper bound. Full design: `docs/scale-and-undo.md` § Retention.

## Gotchas

- The PIM adapter does NOT live here — it lives in `django-pim` (`services/enrichment_adapter.py`),
  duck-typed, self-registering. The bus never imports a source module directly.
- `proposed_value` / `current_snapshot` are opaque to the bus — only the adapter's writer reads
  them. Don't add per-module columns (PIM `feature_idx`, etc.) to `ContentProposal`; they go in
  `target_locator`.
- `EnrichmentTask` stays coarse: `open → done`, no lease/retry/DAG. The moment it grows toward a
  durable job queue, that's `django_workflows` — a separate module, not this one.
- **`scope_spec.module` is required** — `EnrichmentTask` has no `target_module` column, so
  `task_service.resolve_targets` reads the adapter key from `scope_spec["module"]`. The etap-05
  request schema must carry it.
- **`mode: "gap"` ⇄ `find_gaps`** — `resolve_targets` routes `gap` to `adapter.find_gaps` and
  `filter`/`list` to `adapter.resolve_targets`. `filter` is a simplified `gap` (no named
  check). The real PIM `find_gaps` lands in etap-04 / pim-quality-score.
- **`mode: "csv"` is parsed by the BUS, not the adapter (etap-11).** The CSV lives in the bus's own
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
  Undo expires with retention — once the etap-10 beat GC-s the old PIM `Picture`, the way back is gone.
- **Mass apply/undo runs on the `enrichment` Celery queue.** `dispatch_canvas` sets `queue=` per
  signature from `ENRICHMENT_CELERY_QUEUE` (default `enrichment`); the host must run a worker that
  consumes it (`celery -A main worker -Q ...,enrichment`). Re-running a bulk is idempotent — already
  applied → status guard → `failed` bucket, no double-write. Full design: `docs/scale-and-undo.md`.
- **`intake` snapshots via `adapter.read_current`**, never the worker's "before" — drift needs
  ground truth. A new proposal supersedes older `pending` ones on the same
  `(target_module, subject_ref, target_kind, locator_hash)`; `external_ref` makes intake idempotent.
- **Gap-mode resolve is a work queue (etap-13).** `resolve_targets` for `mode:"gap"` routes through
  `resolve_gap_page`: candidates with a `pending`/`drifted` proposal (or `rejected`/`applied` inside
  `cooldown_days`) drop off, and `params.limit` caps the budget (`counts["proposed"] >= limit` →
  empty). The worker re-pulls **page=1 until empty** — `page++` silently skips items. Dedup key is
  the coarse generic `(target_module, subject_ref, target_kind)`, NOT `locator_hash` — don't "fix"
  it; the bus never reads the opaque locator. Non-dict candidates pass through untouched.
- **`SpawnRule.check_key`, not `check`** — `check` shadows `Model.check()` (models.E020), same trap
  GapDefinition hit. The scope_spec key stays `"check"` (cross-module contract, a dict key).
- **The PIM adapter ignores `SpawnRule.params`** — PIM's rule parameters live on the
  `GapDefinition` row keyed by `check_key`; `params` is forwarded per the contract but reserved.
- **`run_rule` dedups against `open` AND `in_progress`** — the generic `spawn()` coalesces only
  `open`; a claimed gap task can run for hours and the beat must not stack a second one behind it.
