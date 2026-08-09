---
title: Data Model
description: EnrichmentTask and proposal rows — the work order and its per-field results.
---

Two models carry the whole bus: a thin work order and its row-per-field results. Both inherit
`BaseModel` (`created_at` / `modified_at` from `django-utils`). The models hold **no business
logic** — transitions, superseding and drift detection live in services (next stage). The state
machines are declared as data in `enums.py` so they're testable and reusable.

## EnrichmentTask

A thin, self-describing work order. "Regenerate 50k descriptions" is **one row**; the worker
resolves targets lazily through the adapter, so the table grows with the number of commands, not
the size of the catalogue.

| Field | Type | Purpose |
|-------|------|---------|
| `type` | Char (indexed) | Category label for display/reporting (`fix-attribute`, `translate`, …). Does not constrain which fields proposals touch. |
| `params` | JSON | Opaque instruction for the worker (n8n). The bus never interprets it. |
| `scope_spec` | JSON | Three modes: `{mode:"filter",…}`, `{mode:"csv",file:…}`, `{mode:"list",refs:[…]}`. |
| `batch_key` | Char (indexed) | Find-or-append: the analyzer coalesces per run instead of one task per product. |
| `status` | Char (indexed) | `open → in_progress → done \| cancelled \| failed`. |
| `requested_by` | FK User (SET_NULL) | Who spawned it. |
| `counts` | JSON | Progress `{targets, proposed, applied, rejected}`. |

`in_progress` is a soft anti-dup marker (worker claimed it) — **not** a lease. No retry, dead-letter,
DAG or priorities. The moment it needs those it's `django_workflows`, a separate module.

## ContentProposal

A row-per-field result with a **generic, module-agnostic target** (decision #11). The bus owns the
shape; it never reads the content.

| Group | Fields | Purpose |
|-------|--------|---------|
| Link | `task` (FK, SET_NULL) | Where the work came from. |
| Target | `target_module` (indexed), `target_type`, `subject_ref` (indexed), `subject_label`, `subject_url`, `target_kind`, `target_locator`, `locator_hash` (indexed) | `target_module` keys the adapter in the registry. `subject_label`/`subject_url` are denormalised so the review queue needs no joins. `target_locator` holds the precise address (PIM `{channel,language,feature_idx}`; CDB `{block,lang}`). |
| Value | `proposed_value`, `staged_file`, `current_snapshot` | Opaque to the bus — only the adapter writer reads them. `staged_file` references a staging store for media; `current_snapshot` powers diff / drift / undo. |
| Metadata | `status` (indexed), `source`, `confidence` (Decimal 0.000–1.000), `batch_id` (indexed), `external_ref` (indexed), `reviewed_by/at`, `applied_at`, `reject_reason` | `external_ref` (worker execution id) gives idempotency. |

### `locator_hash` — why a column

The superseded lookup keys on `(target_module, subject_ref, target_kind, hash(target_locator))`.
`target_locator` is JSON, so it can't sit in a btree index directly. `locator_hash` materialises it:
a sha1 of canonical JSON (`sort_keys=True`, no whitespace) computed in `save()`, so the hash is
independent of key order. It's non-cryptographic — purely a dedup/index key.

There is **no DB unique constraint** on it: superseded rows and one pending row must coexist on the
same target. Enforcing "only one live proposal per target" is the service's job (next stage), which
marks the older row `superseded` when a newer proposal arrives.

> Reminder: `save()` recomputes `locator_hash` from `target_locator`. A service using
> `save(update_fields=[…])` that changes `target_locator` must include `locator_hash` in the list,
> and `bulk_create` (which bypasses `save()`) must set it explicitly.

### State machine

```
pending ─┬─► applied
         ├─► rejected
         ├─► superseded
         └─► drifted ─┬─► applied
                      └─► rejected
```

`drifted` (D2): on apply, the service re-checks `adapter.snapshot` against `current_snapshot`. If the
live value changed since the proposal was made, it does **not** overwrite — it sets `drifted`, and
the CMS shows a re-confirm view (proposed vs the *new* current). The operator confirms (→ applied) or
rejects.

## Why a generic target

PIM-shaped columns (`real_product_sku` / `channel_idx` / `feature_idx`) would weld the bus to PIM.
Enrichment is a horizontal content-quality bus; PIM is just the first client. Keeping the target
generic (`target_module` + `subject_ref` + `target_locator`) is cheap now and saves a nightmare
migration of the core table once a second module (contentdb, blog) needs an adapter. `apply_service`
routes by `target_module` to the registered adapter — never `if target_module == "pim"`.
