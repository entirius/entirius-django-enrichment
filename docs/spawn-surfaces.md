---
title: Spawn Surfaces
description: Five ways an EnrichmentTask is born — API, CSV, spawn rules, CMS, and shell.
---

Every enrichment task is an `EnrichmentTask` born from `POST /tasks/` (or its CSV sibling). The bus
does not care where a task came from — five surfaces feed the same backend, the same
`resolve_targets` pull-loop, the same review queue.

| Surface | When | `scope_spec` | Notes |
|---------|------|--------------|-------|
| **CSV import** | bulk, off-line, ops | `{mode:"csv", module, file:<staged ref>, channel, language, row_type}` | one file = one channel + one language; rows grouped by `type` → N single-type tasks |
| **PIM ProductList** bulk-action | "these 200 from a category" | `{mode:"filter", ...}` or a selection list | munin-gated (etap-06) |
| **PIM ProductDetail** button | "this one, now" | one-SKU `list` | munin-gated (etap-06) |
| **API-analyzer** (n8n loop) | incremental, programmatic | `{mode:"list", refs:[...]}` + `batch_key` | find-or-append: coalesces into one rolling task |
| **Gap spawn** (SpawnRule, etap-13) | quality gaps → tasks, manual "Run now" or auto beat | `{mode:"gap", module, check, params, scope}` + `batch_key="gaprule:<rule>"` | lazy `find_gaps` resolve; dedup/cooldown filter; limit budget |

## CSV import (`POST /tasks/import-csv/`)

Multipart. The `file` part is a CSV; the rest is metadata (`channel`, `language`, optional `params`).

**Format — exactly three columns, nothing else:**

```csv
sku,field,type
ENT-S001,description,fix-attribute
ENT-S002,description,fix-attribute
ENT-S001,name,translate
```

- `sku` → `subject_ref`. `field` → the target field the worker maps to a module locator
  (`feature_idx` for PIM). `type` → the **work-type** (`task.type`: `fix-attribute`, `translate`,
  `fill`, …).
- UTF-8 (a leading BOM is tolerated), comma-separated.
- `channel` / `language` come from the upload dialog — one file = one channel + one language.
  Per-row `channel`/`language` columns are deliberately out of scope (a future "mixing" feature);
  an extra column is rejected, not silently ignored.
- SKU **existence** is not checked at upload (that would cost a PIM lookup per row — death at 50k).
  Only the shape is validated (header + non-empty cells). Bad SKUs surface later, when the worker
  resolves targets.

**Grouping.** Rows are grouped by `type` → **one task per distinct work-type** (single-type tasks
fit the pull model). The CSV above yields two tasks: `fix-attribute` (2 rows) and `translate` (1).
All N tasks share **one** staged file; each streams only its own rows.

**Streaming (no OOM).** The file is staged once in the bus's `staging_store`. The bus — not the
adapter — parses it: `task_service.resolve_targets` reads the staged CSV line by line for
`mode:csv`, filters to the task's `row_type`, and returns one page (`CSV_PAGE_SIZE`) of
`{subject_ref, field}` at a time. The source adapter never imports `staging_store` (decision D1);
CSV is a generic bus concept, not a PIM one.

**Cleanup.** The retention beat (etap-10) drops the staged CSV when its done tasks age out — guarded
so a slower work-type still `open`/`in_progress` keeps the shared file alive (`csv_staged_deleted`
in the prune counts).

**Throttle.** `POST /tasks/` and `import-csv/` carry the `enrichment_spawn` throttle (120/min
default) — bounds a leaked service-account token or a runaway loop. Admin-JWT only.

## API-analyzer (`batch_key` find-or-append)

The n8n analyzer scans the catalogue and proposes work incrementally. Spawning one task per product
would flood the queue, so the analyzer passes a **`batch_key`** (one per run). `task_service.spawn`:

- No open task with that `batch_key` → create one.
- An open task exists and the call is `mode:list` → **append** the new `refs` (deduped,
  order-kept) to that task — one rolling task absorbs the whole run.
- An open task exists for a non-`list` mode → return it unchanged (nothing to append).

Coalescing is bounded to **open** tasks: once a run's task is marked done, the next `batch_key`
collision starts a fresh task.

**Convention.** The analyzer owns the key. A readable, collision-free shape is
`analyzer:<workflow>:<run-id>` — but the bus treats `batch_key` as an opaque string; any stable
per-run value works.

## The pull-loop (all surfaces converge here)

```
POST /tasks/  (or import-csv/)        spawn — one or N EnrichmentTasks
GET  /tasks/?status=open              worker picks up open tasks
GET  /tasks/{id}/targets/?page=       lazy resolve (CSV streamed, list/filter via adapter)
POST /proposals/                      worker submits a proposal per target/field
PATCH /tasks/{id}/  {status:done}     worker closes the task
```

## Gap spawn (`SpawnRule`, etap-13)

A `SpawnRule` maps one module-side quality check (`check_key` — for PIM that's a
`GapDefinition.key`; a plain string, no FK, no local validation) to a work-type. Two triggers run
a rule through `spawn_rule_service.run_rule`:

- **Run now** — `POST /spawn-rules/{key}/run/` (the CMS button; `enrichment_spawn` throttle).
- **Auto** — the `enrichment.gap_autospawn` beat (host-scheduled, e.g. every 30 min) runs every
  `auto=True, active=True` rule; one broken rule never kills the pass.

`run_rule` is the dedup choke point:

1. `batch_key = "gaprule:<rule.key>"` — deterministic, composed by the service (never the CMS).
   An `open` **or `in_progress`** task with that key short-circuits to `already_running` (the
   generic `spawn()` coalesces only `open`; a claimed gap task can run for hours).
2. **Probe:** page 1 is resolved (through the filter below) before spawning — empty →
   `no_candidates`, no task spam.
3. The task carries `params.limit` (budget) and `params.cooldown_days`, NULL on the rule =
   `ENRICHMENT_SPAWN_DEFAULT_LIMIT` / `ENRICHMENT_SPAWN_COOLDOWN_DAYS`.

### Gap-mode resolve = a work queue (page 1 until empty)

`task_service.resolve_gap_page` post-filters the adapter's candidates: targets with a `pending`
or `drifted` proposal are dropped always; `rejected` blocks for `cooldown_days` from
`reviewed_at`; `applied` blocks for `cooldown_days` from `applied_at`; `superseded`/`reverted`
never block. Dedup key = the **generic** pair `(target_module, subject_ref, target_kind)` —
the bus never reads the opaque locator. Once `counts["proposed"]` reaches `limit`, the queue
reads empty and the worker marks the task done; the next beat pass spawns the next batch.

**Worker contract:** because consumed targets drop off the front, a gap-mode worker re-pulls
**`page=1` until it comes back empty** — `page++` would silently skip items. A failed generation
must be submitted as a low-confidence proposal with the error note (a silent skip re-surfaces the
same target forever).
