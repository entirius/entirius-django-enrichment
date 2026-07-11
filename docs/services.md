# Service layer — contracts

The service layer is the bus's brain: framework-agnostic (Pydantic/primitives in, models out, zero
DRF), it owns the state machine, intake, review, and the single write choke point. Source-module
specifics never leak in — they live behind an adapter loaded from a registry.

Dependency direction: `api → services → models`. `apply_service` and `proposal_service` reach
source modules only through `adapters.get_adapter` — never an `import django_pim`.

## Adapter contract (`adapters/base.EnrichmentAdapter`)

> Writing an adapter for a new source module? The canonical step-by-step recipe is the
> [Adapter Guide](https://docs.entirius.com/volkanos/modules/enrichment/adapter-guide/) in
> entirius-docs. This section is the in-repo contract reference.

An adapter is a **module** exposing five duck-typed callables (plus an optional sixth,
`release_undo_anchor`, for cleanup-time GC of an external undo anchor — etap-10). The Protocol is
documentary (not enforced) so a source module registers a dotted path without importing anything from
`django-enrichment`. The first real adapter is PIM —
`django_pim.services.enrichment_adapter` (etap-04, see django-pim `docs/enrichment-adapter.md`);
the test fake (`tests/fake_adapter.py`) implements the contract in memory for the bus's own suite.

| Callable | Side | Role |
|---|---|---|
| `resolve_targets(scope_spec, page=1) -> list` | READ | `filter`/`csv`/`list` scope → concrete targets (lazy/paged) |
| `find_gaps(check, params, scope) -> list` | READ | named quality check → candidates (`mode: "gap"`) |
| `read_current(*, subject_ref, target_kind, target_locator) -> dict` | READ | authoritative current value → `current_snapshot` (diff/drift/undo) |
| `apply(proposal) -> None` | WRITE | write `proposed_value` into the module — the only write path |
| `revert(proposal) -> None` | WRITE | restore `current_snapshot` (undo, etap-09) |

**Registry (DIP).** `get_adapter(target_module)` reads `settings.ENRICHMENT_ADAPTERS` on every call
(a snapshot taken at import time would miss the PIM adapter's later self-registration — finding
etap-01), lazy-imports the dotted path, and caches by path. Missing key → `ValueError`.

```python
ENRICHMENT_ADAPTERS = {"pim": "django_pim.services.enrichment_adapter"}
```

## `scope_spec` shape

`EnrichmentTask` has no `target_module` column (model frozen in etap-02), so the adapter key and the
resolution mode live in the opaque `scope_spec` JSON, interpreted by `task_service`:

```python
{"mode": "list",   "module": "pim", "refs": ["SKU-1", ...]}
{"mode": "filter", "module": "pim", ...module-side filter keys...}
{"mode": "csv",    "module": "pim", "file": "<staging ref>"}          # streamed in etap-11
{"mode": "gap",    "module": "pim", "check": "missing_feature", "params": {}, "scope": {}}
```

`module` is **required** (keys the registry). The etap-05 request schema must carry it.

### `filter ⇄ gap` (decision PO, etap-03)

`filter` and `gap` are the same family — find targets via a module-side query. `resolve_targets`
routes `gap → adapter.find_gaps` and `filter`/`csv`/`list` → `adapter.resolve_targets`. `filter` is a
**simplified `gap`** (no named check). The `gap` mode is wired into the contract now; the real PIM
`find_gaps` catalogue lands in etap-04 / `pim-quality-score`. The test fake implements both, so the
routing is proven at this stage.

## `task_service`

| Function | Contract |
|---|---|
| `spawn(*, type, scope_spec, params=None, requested_by=None, batch_key="")` | Validate `scope_spec` (mode ∈ {filter,csv,list,gap} + `module`). With a `batch_key` matching an OPEN task: `list` mode appends refs (deduped, order kept — analyzer coalescing), other modes return the existing task. Else create. |
| `list_open(type=None)` | OPEN tasks, oldest first (worker pull). |
| `resolve_targets(task, page=1)` | Dispatch by `mode` to the adapter (lazy/paged). `filter`/`csv`/`list` → `resolve_targets(scope_spec, page)`; `gap` → `find_gaps(check, params, scope)` with `page` injected as `scope["page"]`. The adapter MUST bound its result to the page — never return the whole catalogue. |
| `transition_status(task, new_status)` | Enforce `TASK_STATUS_TRANSITIONS`. |
| `mark_in_progress(task)` | `→ in_progress` (worker claims an open task before resolving). |
| `mark_done(task)` | `→ done` (legal only from `in_progress` — call `mark_in_progress` first). |
| `increment_count(task, key, by=1)` | Bump a `counts` progress counter. |

## `proposal_service`

| Function | Contract |
|---|---|
| `intake(*, target_module, subject_ref, target_kind, target_locator, proposed_value, task=None, ...)` | Idempotent on `external_ref`. Snapshots the live value via `adapter.read_current` (never the worker's "before"). Supersedes older `pending` proposals on the same `(target_module, subject_ref, target_kind, locator_hash)` — `locator_hash` computed explicitly (the model `save()` is bypassed by bulk paths). Bumps `task.counts["proposed"]`. |
| `transition_status(proposal, new_status, *, user=None, reason="")` | Enforce `PROPOSAL_STATUS_TRANSITIONS`. Stamps `applied_at`/audit on `applied`, audit + `reject_reason` on `rejected`. |
| `list_for_review(*, status=pending, module=None, target_kind=None, batch_id=None, source=None, confidence_min=None, search=None, ordering="-created_at")` | Server-side filtered/ordered queue (`search` → `subject_label__icontains`). |
| `accept(proposal, *, user=None)` | Delegate to `apply_service.apply` (drift-aware write). |
| `reject(proposal, reason, *, user=None)` | `→ rejected` + reason + audit. |
| `apply_many(proposal_ids, *, user_id=None)` | Sync core: per-row apply, returns `{applied, drifted, failed}`. Called by the Celery task and directly by tests. |
| `bulk_accept(filters, *, user=None)` | **Async** — resolve filtered `pending` ids, dispatch `apply_proposals_task.delay(ids, user_id)`, return `{enqueued}`. Constrained to `pending` (drifted needs the conscious per-item re-confirm, D2). |
| `bulk_reject(filters, reason, *, user=None)` | **Sync** — single `.update()` over pending rows, returns `{rejected}`. |

## `apply_service` (write choke point + drift, D2)

`apply(proposal, *, user=None)` routes by `proposal.target_module` to the adapter — zero
`if target_module == "pim"`. Drift contract:

- `pending` + live value diverged from `current_snapshot` → refresh `current_snapshot`, mark
  `drifted`, **do not write** (CMS re-confirm view, etap-06).
- `drifted` being accepted → the operator re-confirmed: refresh `current_snapshot` to live, then
  write.
- no drift → `adapter.apply(proposal)` → `applied` (+ `applied_at`, `reviewed_by`).

The module write and the status flip run in one `transaction.atomic()` — a crash between them would
leave the source mutated but the proposal still pending (double-apply risk). In-process adapters
share this transaction; an adapter doing external non-DB writes can't be rolled back (its concern).

The PIM adapter's `apply()` body lives in `django-pim` (etap-04). The bus suite still drives drift +
apply through the in-memory fake (no django-pim dependency in this module's tests); the real PIM
read-merge-write + inheritance-override path is tested in django-pim `tests/test_enrichment_adapter.py`.

## Async note

`bulk_accept` dispatches `tasks/apply_tasks.apply_proposals_task` (a one-liner over `apply_many`).
The host service registers the task and its queue and wires `config_from_object`. Tests force the
current Celery app eager (`conftest._celery_eager`). Mass apply via Celery canvas (chunks/chords +
`bulk_update` over `iterator()`, 10k+ without OOM) is etap-09; batch-intake (throttle, 100k POSTs) is
etap-09 too.
