---
title: Admin API
description: v2 admin API for enrichment tasks, proposals, review, and apply.
---

v2 admin API under `/api/enrichment/v2/admin/`. JWT + `IsAdminUser` on every ViewSet (the n8n
worker authenticates with an admin service account — no separate auth tor). Pydantic v2 schemas,
v2 error envelope, standard pagination. Views are thin: parse → service → Response; all ORM lives
in the services.

Access areas: `enrichment.proposals` for tasks, proposals, corrections and templates; `enrichment.profile` for the
company profile, memory and rule promotion; `enrichment.rules` for spawn rules. Templates stay in the proposals
area by design (contract A3 § 4: operators maintain the presets they spawn from, and the list is read in the spawn
dialog — an area is per route, so writes cannot be split from that read). Template `rules` / `params.prompt` reach
only the tasks spawned from that template, never every prompt.

## Endpoints

Error envelope `{error, message, debug_id, details}`, pagination `count/next/previous/results`.

| Method | Endpoint | Service | Notes |
|--------|----------|---------|-------|
| `GET` | `usage/month/` | `ai_usage_service.current_month` | This month's AI usage of the instance's toolbox client (CMS header counter) — see § AI usage this month |
| `GET` | `workers/?target_module=` | `worker_service.list_active` | Active workers, plain list `[{key, label, kind, target_module, mode, params_schema}]` |
| `POST` | `tasks/` | `run_service.spawn` → `task_service.spawn` | Spawn a work order. `scope_spec.module` required. `params` validated by the kind when an active Worker owns `type` (400 otherwise). `template: <key>` instead of (or matching) `type` — see § Templates. `lane: now \| queue` (omitted = by size), subjects already in flight skipped — see § Run lanes. Throttled (`enrichment_spawn`, 120/min). → 201 task + `skipped_in_flight`; 409 `ALREADY_RUNNING` |
| `POST` | `tasks/{id}/retry/` | `retry_service.retry` → `run_service.spawn` | Empty body. A new task for the subjects the finished task failed on — see § Retry a task. Throttled (`enrichment_spawn`). → 201 task + `skipped_in_flight` (as `POST tasks/`); 404; 409 `NOT_RETRYABLE` / `ALREADY_RUNNING`; 400 workflow step |
| `POST` | `tasks/import-csv/` | `task_service.import_csv` | Multipart CSV (`sku,field,type`) → N tasks grouped by `type`. One file = one channel + one language. Streamed lazily from one staged file. Throttled. → 201 |
| `GET` | `tasks/` | `task_service.list_tasks` | Filter `type`/`status` (one or a comma list)/`batch_key`/`target_module`/`subject_ref`, paginated; every task carries `lane` and `queued_ahead` |
| `GET` | `tasks/{id}/` | `task_service.get_task` | Retrieve |
| `GET` | `tasks/{id}/targets/?page=` | `task_service.resolve_targets` | Lazy worker pull (adapter-resolved) |
| `PATCH` | `tasks/{id}/` | `task_service.transition_status` | Drive the state machine (e.g. `in_progress`, `done`) |
| `GET/POST` | `templates/?target_module=` | `template_service` | Plain list ordered by `position` (inactive included) `[{key, label, worker_key, worker_label, kind, target_module, params, rules, position, active}]`; create `{key, label, worker_key, params, rules, position, active}` → 201 |
| `GET/PATCH/DELETE` | `templates/{key}/` | `template_service` | `key` immutable; DELETE → 204 |
| `GET` | `fields/?target_module=&channel=&language=` | `catalog_service.describe_fields` | Adapter field catalog `[{key, label, type, max_length, translatable, options?, options_truncated?}]`; 404 `NOT_SUPPORTED` without `describe_fields` |
| `GET` | `languages/?target_module=&channel=` | `catalog_service.channel_languages` | `["pl", "en", …]` (main first); 404 `NOT_SUPPORTED` without `channel_languages` |
| `POST` | `subjects/resolve/` | `catalog_service.resolve_subjects` | `{module, channel, refs}` (1–500 refs, each ≤ 128 chars) → `{known: [{ref, label}], unknown: [ref]}`: refs trimmed, blanks dropped, deduped, request order kept; the CMS validates pasted SKUs with it. 404 `NOT_SUPPORTED` without the adapter extra `subject_labels`, 404 for an unknown channel. Throttled (`enrichment_lookup`, 60/min) |
| `GET` | `subjects/pictures/?module=&channel=&ref=` | `catalog_service.subject_pictures` | `[{ref, label, url, is_main}]` of one item, main first; the CMS picks the picture a `video` run animates (`params.picture_ref`). 404 `NOT_SUPPORTED` without the adapter extra `list_pictures`, 404 for an unknown item or channel. Throttled (`enrichment_lookup`) |
| `GET/POST` | `spawn-rules/` | `spawn_rule_service.list_rules`/`create_rule` | SpawnRule CRUD. Filters: `active`, `search`. → 200/201 |
| `GET/PATCH/DELETE` | `spawn-rules/{key}/` | `spawn_rule_service` | `key` immutable (schema drops it) |
| `POST` | `spawn-rules/{key}/run/` | `spawn_rule_service.run_rule` | Throttled (`enrichment_spawn`). → 201 `spawned` / 200 `already_running` / 200 `no_candidates` |
| `POST` | `proposals/` | `proposal_service.intake` | Worker intake — idempotent on `external_ref`. Throttled (`enrichment_intake`, 100/min). → 201 |
| `POST` | `proposals/upload-media/` | `proposal_service.intake_media` | Multipart media intake — stages the binary, `target_kind=picture`. Size + MIME caps. Throttled. → 201 |
| `GET` | `proposals/{id}/staged-file/` | `staging_store` | Stream a proposal's staged binary (admin-only) — the CMS "after" preview; `?variant=k` = picture `k` of a cover's bundle (`image/png`, 404 for a missing one) |
| `GET` | `proposals/` | `proposal_service.list_for_review` | Filters: status/module/kind/batch/source/confidence_min/search/subject_ref/`task` (id); `ordering` allowlisted |
| `GET` | `proposals/{id}/` | `proposal_service.get_proposal` | Retrieve |
| `PATCH` | `proposals/{id}/` | `proposal_service.update_value` | Body `{proposed_value}` — pending/drifted only, else 400. Creates a `user` revision |
| `GET` | `proposals/{id}/revisions/` | `revision_service.list_revisions` | Plain list `[{number, value, instruction, scope, author_kind, author, cost_usd, profile_versions, warnings, created_at}]` |
| `POST` | `proposals/{id}/revise/` | `revise_service.revise` | `{instruction?, quick_action?, scope?}` → 201 revision, 200 `{candidates}` for `variants`, 200 `{warnings}` for `check_standard` (`qa_checklist`, no revision); 400 / 422 `REVISION_NOT_APPLICABLE`; toolbox errors mapped. Throttled (`enrichment_bulk`) |
| `POST` | `proposals/{id}/check/` | `qa_service.recheck` | Re-run the deterministic QA checks on the current value → 200 proposal |
| `POST` | `proposals/{id}/facts/suggest/` | `memory_service.suggest_fact` | "Add to facts" `{value, label?}` → 201 memory `fact_suggestion` (promote it with `memory/{id}/promote/`) |
| `POST` | `proposals/{id}/revisions/{n}/restore/` | `revision_service.restore` | → 201 `restore` revision copying `n` (404 unknown `n`) |
| `PUT` | `proposals/{id}/rules/` | `rules_service.set_rules` | `{rules: [str]}` ≤ 20 × ≤ 200 chars → 200 proposal |
| `POST` | `proposals/{id}/rules/promote/` | `rules_service.promote` | `{rule, target: template\|worker\|profile}` → 200 `{target, rules}`; template = the task's `params.template` (none → 400); `profile` leaves a memory `rule_suggestion` (rules = the channel's active suggestions). Area `enrichment.profile`: a worker or template rule enters every prompt of that worker / template, as strong as the profile's `always_rules` |
| `POST` | `tasks/{id}/revise/` | `revise_batch_service.queue` | `{instruction?, quick_action?}` (no `variants`, `check_standard`, `fix_warnings`) → 202 `{queued}`; Celery `enrichment.revise_task`. Throttled. One batch per task at a time (400 while one runs; the lock ends with the batch or after `ENRICHMENT_REVISE_LOCK_SECONDS`, 3600); at most `ENRICHMENT_REVISE_BATCH_MAX` (200) pending proposals, else 400 |
| `POST` | `proposals/{id}/accept/` | `proposal_service.accept` | Drift-aware apply via the adapter; approves a workflow step (400 `ANSWERS_MISSING` for unanswered questions). Body `{mode?}` (`add` \| `replace`, one of the proposal's `apply_modes`; default `add`). Response `result_ref` = what the apply created |
| `GET` | `workflows/` | `workflow_queries.list_definitions` | Active workflows — see `docs/workflows.md` |
| `GET/POST` | `runs/` | `workflow_service.start` | Runs list / start `{definition, inputs, channel}` → 201. Throttled |
| `GET` | `runs/{uid}/` | `workflow_queries.get_run` | Run with steps state and cost |
| `POST` | `runs/{uid}/steps/{key}/regenerate/` | `workflow_service.regenerate` | `{instruction?}` → 200. Throttled |
| `POST` | `runs/{uid}/cancel/` | `workflow_service.cancel` | → 200 |
| `GET/PUT` | `profile/?channel=` | `profile_service` | Company profile scope; PUT `{data, note?}` → new version, schema errors 400 per `data.<path>` — see `docs/profile.md` |
| `GET` | `profile/versions/`, `profile/status/`, `profile/schema/` | `profile_service` | Versions, completeness, JSON Schema; `POST profile/versions/{n}/restore/` → new version |
| `GET/PATCH/DELETE` | `memory/`, `memory/{id}/` | `memory_service` | Memory list (filters `channel`/`kind`/`subject_ref`/`status`), dismiss / re-activate, delete |
| `POST` | `memory/{id}/promote/` | `memory_promotion.promote` | `{target?}` → 200 `{entry, profile}` (new profile version) |
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

Scope `{"type": "selection", "text"}` must occur exactly once in the current text (else 400); an answer
that changes text outside the selection is 422, nothing saved. The write re-checks the proposal under a row
lock: accepted/rejected meanwhile → 400; value edited meanwhile (PATCH, another correction) → 422.
Batch: each pending text proposal of the task gets one call; failures land in `task.errors`
(`REVISION_NOT_APPLICABLE` or the toolbox code), counts `revised` / `failed`.

## QA checks (contract A4 § 4)

Every revision of a `{"text"}` or `{"data"}` value (intake's revision 1, corrections, manual edits, restores) is
checked by `qa_service.check`; the result is the revision's `warnings` and, for the latest revision, the proposal's
`warnings`: `[{code, message, span: {start, end, text} | null, path: str | null}]` (≤ 50; offsets into the text).
A `{"data"}` value (a JSON step: metadata, search phrases) is checked field by field: `LIMIT` against the step's
`params.json_schema`, then `rule`, `NUMBER_NOT_IN_FACTS` and `FACT_NOT_IN_PROFILE` on every string, each warning
with the field's `path` (`description`, `faq[2].answer`) and its span inside that field; `path` is null for a text
value, which gets `LIMIT` first when its field has a `write` worker character range (§ Field length).

| Code | Raised when |
|---|---|
| `rule` | an active profile `rules` pattern matches (`flags` `i m s x`), or a `language.spellings` `wrong` occurs (whole word, any case). A pattern that does not compile disables only its rule and logs a warning |
| `NUMBER_NOT_IN_FACTS` | a number of the text matches no number of the profile facts registry (`id`, `label`, `value` — a citation's id never counts as a new number), the task's source params (`prompt`, `brief`, `inputs` only — configuration such as `limit` never counts) or the subject's stored context numbers — once per distinct number |
| `FACT_NOT_IN_PROFILE` | the text cites `(profile fact: <id>)` and `<id>` is not an `id` of the merged profile's `facts` registry — once per distinct id, `span` on the whole citation. The format is a contract with the workflow prompts that ask the model to label claims with their source (the `id` column of the facts table in the prompt); case and the whitespace around the colon and the id are free, the id is compared exactly. An empty registry flags every citation |
| `checklist` | `check_standard` found a failed checklist item (`span` only when the quoted words occur in the text) |
| `LIMIT` | a `{"data"}` field breaks the step's `json_schema`: a string over `maxLength` / under `minLength`, an array over `maxItems` (`span` null). The schema goes to the provider with its limits as hints only (`toolbox_calls.provider_schema`), so they are enforced here. A `{"text"}` value outside the `field_limits` range of its field (`write` worker options): `195 characters, the limit is 160.` / `100 characters, at least 120.` (`span` and `path` null) |

Numbers (`qa_numbers`) are compared by value: integers, decimals with a comma or a dot, thousands grouped by
space / no-break space / comma / dot, percentages and ranges (each end separately). `12,5 %` = `12.5%`,
`1 299,99` = `1299.99`; an ambiguous `1,299` means 1299 or 1.299. Digits glued to a letter (`A4`, `B2B`) and
HTML tags / entities are skipped. The source context is recorded when a kind builds it:
`extras.context_block(..., task=)` (`write`, `pick`), `field` and `translate` (source texts) call
`seams.record_context` → `params.context_numbers = {subject_ref: [values]}` on the task (row-locked).

Quick actions on `POST proposals/{id}/revise/`: `check_standard` — one completion (kind tag `check`) with the
merged profile's `checklists.text` as instructions and the text as data, answer `{problems: [{message, quote}]}`
→ `checklist` warnings stored next to the deterministic ones, no revision (no checklists → 400);
`fix_warnings` — a correction with a fixed instruction and the current warnings as a fenced data block
(none → 400); its new revision is re-checked. `POST proposals/{id}/check/` re-runs the deterministic checks
(e.g. after a profile change) and drops checklist warnings. Both store warnings row-locked against the value
they were computed on: a proposal no longer pending/drifted → 400, a value changed meanwhile → 422
`REVISION_NOT_APPLICABLE` (nothing saved).

## Generic kinds `write` / `pick` (contract A3 § 3)

Both use the adapter extras with fallbacks (`kinds/extras.py`): `describe_fields` (absent → no catalog:
labels = keys, no limits), `channel_languages` (resolves `languages: ["*"]`; absent → subject error
`NOT_SUPPORTED`), `make_locator` (absent → `{channel, language, field}`), `field_key` (absent → `locator["field"]`),
`build_context` (absent → `{}`, sent as an `<item_context>` data block the context cannot close). `write` also
narrows the languages to `subject_languages` (absent → all requested; no overlap → the subject yields nothing, no failure). Catalog and
channel languages are asked once per language per task, not per subject.

| Kind | Params | Calls | Drafts |
|---|---|---|---|
| `write` | `{channel, fields, languages, prompt, rules}` (+ `augment`, `seo_scope`); options `{rules, field_rules, field_limits}` | one per subject × language, plus one rewrite when a short field comes back outside its `field_limits` range (§ Field length); schema = one string per field (`maxLength` from the catalog, or the `field_limits` range) | one per field × language, `{"text"}`, `html` fields sanitised |
| `pick` | `{channel, fields, prompt, rules}` (+ `augment`, `seo_scope`); options `{rules}` | one per subject; schema = enum of option values per field (array for multiselect) | one per field, locator language `None`, `{"option"}` / `{"options"}` |

A field of the wrong type (select in `write`, non-select or no catalog in `pick`) fails the subject with
`FIELD_TYPE` in `task.errors`. `translate` takes an optional `params.fields` that overrides the worker's.

### Field length (`write` option `field_limits`)

A model does not count characters: a prose `field_rules` entry ("140-160 characters") alone landed five meta
descriptions at 195 characters on average. `options.field_limits` gives a field a number range, keyed by the
catalog field key: `{"meta_description": {"min": 120, "max": 160}, "meta_title": {"min": 40, "max": 60}}`.
`max` is required, `min` defaults to 0 (`at most max`) and must not exceed `max`; spaces count. Keep `field_rules`
for the content guidance; the range is the contract.

- **Prompt and schema:** the field line reads `between 120 and 160 characters, spaces included` (replacing the
  catalog's `at most N characters`), and the answer schema carries `minLength` / `maxLength`, which the provider sees
  as a description hint (`toolbox_calls.provider_schema`).
- **One corrective call:** when a field with `max` up to 300 comes back outside its range, the run makes one more
  completion for just those fields: the same selector tag (`enrichment.write`) and answer shape, the worker's system
  prompt and rules, the current values as data, each field's range and how far the value is from it. What comes back
  is kept, even if it is still outside. Longer fields are never rewritten. A subject × language costs at most two
  completions; both are charged to the task and shared by the drafts. A failed rewrite keeps the first values, never
  fails the subject, and leaves the drafts' cost unknown (`None`, the task `cost_complete` false). The
  `enrichment.queue` log has `event=field_rewrite fields=… still_out=<n>` or `event=field_rewrite_failed code=…`.
- **QA:** a `{"text"}` proposal whose field has a range in the `options.field_limits` of the worker that produced it
  (`source` = `worker:<key>@v<n>`) gets a `LIMIT` warning when it is outside, on intake and on every later revision
  (an operator's edit too), so the review shows a miss the rewrite left.

## Augmentations (contract A4 § 5)

An augmentation is a deterministic research step that runs before a kind's completion and adds a fenced data
block to its prompt — the model never calls a tool itself. Registry: `register_augmentation(key, fn)` in
`django_enrichment.augmentations`, `fn(*, task, subject_ref, context) -> AugmentResult(block, cost,
memory_writes)`; `run_all` fences each block under its key and saves the memory writes. Built in: `seo_brief` and
`cover_product` (the `build_context` facts of the product in `brief.cover_product`, none without one; an unknown
product fails the subject `PRODUCT_NOT_FOUND`), registered in `EnrichmentConfig.ready()`. The profile's
`seo.exclude_terms` keep phrases out of `seo_brief` (seeds, keyword ideas, the shown evidence; a remembered brief is
shown against the current list).

- **Enabling:** `params.augment: [keys]` on `POST tasks/` or in a template's `params` (templates may preset it).
  Keys are validated against the registry — an unknown key → 400 (a template with one is refused too).
  `augment`, `seo_scope` (`subject | category`) and `seo_detail` (`brief | full`) survive the kind's params
  validation, like `template`.
- **Where blocks go:** `write` (per subject × language — each language's prompt gets the block researched in that
  language), `pick` (the channel's main language), and the workflow step kinds (`generate` / `questions`,
  `kinds/step.py`). In `write` / `pick` the augmentation's context is the adapter's `build_context` for the subject
  plus `language`; in a step it is `{brief, inputs}` (no language → the toolbox market defaults).
- **Memory:** the prompt's `memory` block leaves out the memory kinds the task's augmentations supply (an
  augmentation key names the memory kind it writes), so a brief appears once.
- **Costs:** every toolbox call of an augmentation charges the task (cost ledger, `seo` and `completion` in
  `cost_breakdown`); proposals carry only the cost of the call that produced them.

**`seo_brief`** (`augmentations/seo_brief.py`, prompts in `seo_prompts.py`):

1. Reuse — the newest active, unexpired memory `seo_brief` in the same language (`data.language`) about the
   subject, then its category (adapter extra `category_of`) → that brief, cost 0, no call.
2. Research — one completion proposes 3-5 seed phrases from the context (selector `enrichment.seo_brief.seeds`;
   a workflow step's context — it has a `brief` — gets the topic prompt: 2-4 word head terms, never questions, for
   the keyword tool to expand into the long tail; an item keeps the item prompt);
   toolbox SEO `search-volume/` and `keyword-ideas/` (one call each, all seeds) and `serp/` (depth 10) for the 3
   seeds with the highest volume, all through `toolbox_calls.seo` (`ToolboxClient._post`, `retry=False`; the
   toolbox cache answers repeats at cost 0). Every SEO call carries `language_code` = the context's `language`
   (blank → omitted); the location is the toolbox default.
3. Evaluation — one completion (selector `enrichment.seo_brief.evaluate`), intent before volume: SERP intent must
   match the page, winnability, gaps, volume only breaks ties → `{primary, secondary (≤ 2), intent, serp_notes,
   gaps}` plus `search_volume` of the chosen phrases (from the data, not the model), `language` and `evidence`
   (the data the evaluation saw: `candidates` = seeds with volume, `ideas` = the top 20 keyword ideas, `serp` per
   phrase = top 10 organic results, people also ask, AI overview flag).
4. Memory — `seo_brief`, expires after 90 days, under the subject's category when the scope is `category`
   (`params.seo_scope`; the default for kind `write`, i.e. product descriptions — every product of the category
   reuses one brief), else under the subject; a subject without a category keeps it on the subject.
5. Block — the short brief (primary, secondary, intent, volumes, SERP notes, gaps); with `params.seo_detail: full`
   also the evidence (`render_evidence`: monthly volumes of seeds and ideas, then each SERP with its people also
   ask) — the article steps use it to ask and write from the research. A reused brief brings its evidence at cost
   0 (one research per run feeds every step); a brief remembered before `evidence` existed renders short. The
   memory text stays the short brief.

A toolbox failure fails the subject with `SEO_BRIEF_<toolbox code>` (e.g. `SEO_BRIEF_PROVIDER_ERROR`); an answer or
SEO `data` that does not fit the boundary models (`augmentations/seo_answers.py`) with `SEO_BRIEF_BAD_RESPONSE`;
no seed phrases → `SEO_BRIEF_NO_SEEDS`. Any other error (a misconfigured channel, a bug) reaches the runner as
`INTERNAL_ERROR` with an error id. Tasks without the augmentation see remembered briefs in their `memory` block.

## Templates (contract A3 § 4)

`TaskTemplate` = a saved worker run: `params` (validated against the worker kind's `Params`, `channel`
optional), `rules` (≤ 20 × ≤ 200 chars), `position`, `active`. `POST tasks/` with `template`: `type` = the
template's worker key (a different `type` → 400), params = template params ∪ request params (request
wins), `rules` = template rules + request rules, `params.template` keeps the key. Unknown or inactive
template, or an inactive worker → 400. Corrections read the template's current rules (proposal → template
→ worker) and promotion with `target: "template"` appends to them.

## Run lanes (plan 22)

Two Celery lanes for the internal runner: `now` → `ENRICHMENT_INTERACTIVE_QUEUE` (`enrichment_interactive`), `queue`
→ `ENRICHMENT_BULK_QUEUE` (`enrichment_bulk`; the host runs its worker with concurrency 1, so a bulk run never blocks
a click). `queue_dispatch` (planner and consumer) routes by `task.lane` (`lane_service.celery_queue`). Workflow steps
always spawn on `now`; batch corrections (`tasks/{id}/revise/`) always run on the interactive queue. External workers
ignore the lane.

`POST tasks/` request field `lane`:

| `lane` | Result |
|---|---|
| omitted / `null` | by size: a list scope with exactly 1 subject → `now`, anything else (more subjects, filter / gap / csv) → `queue` |
| `"now"` | `now`; more than `ENRICHMENT_RUN_NOW_MAX_SUBJECTS` (20) subjects, or a scope that is not a list → 400 `Run now takes at most 20 items at a time. Add this run to the queue instead.` |
| `"queue"` | `queue` |
| anything else | 400 (schema) |

**In-flight dedupe** (`run_service`, `POST tasks/` only — internal spawns, CSV import and calls with `batch_key` are
not deduped): a subject of a list scope is skipped when an `open` / `in_progress` list-mode task of the same type and
module carries it with the same fingerprint — `params.template`, channel (`params.channel`, else
`scope_spec.channel`), fields (`fields`, else `field`) and languages (`languages`, else `target_languages`), lists
compared as sets. The spawned task's `scope_spec.refs` hold only the rest; the response lists the skipped refs in
`skipped_in_flight`. The cap counts the remaining subjects. Every subject skipped → nothing spawned, 409:

```json
{"error": "ALREADY_RUNNING", "message": "This run is already in progress for all 3 items. Wait for it to finish.", "debug_id": "…", "details": []}
```

(one subject: `This run is already in progress for this item. Wait for it to finish.`). Pending proposals keep
their supersede behaviour.

**Task responses** (`GET tasks/`, `GET tasks/{id}/`, `POST tasks/`) carry `lane` (`now` | `queue`) and
`queued_ahead`: while the task is `open` and its worker is an active internal one, the number of open internal tasks of
the same lane created before it (0 = next); `null` once it runs, after it finished, or for an external worker.
`POST tasks/` adds `skipped_in_flight: [refs]` (empty when nothing was skipped).

## Retry a task

`POST tasks/{id}/retry/` (empty body) runs the failed part of a finished task again as a **new** task — the way
`POST tasks/` spawns one (`run_service.spawn`): params validation, run lane, in-flight dedupe, work-queue planning,
`requested_by` = the caller. The old task stays as it was. The answer is the `POST tasks/` answer: 201, the new task
and `skipped_in_flight`.

- **Subjects:** the distinct `subject_ref`s of the task's `errors`, as a list scope of the same module
  (`{mode: "list", module, refs}`; a list scope keeps its other keys, any other scope keeps `channel`). `errors` is
  capped (`ERRORS_CAP`, 20), so a run with more failures retries the ones recorded. A **failed** task whose errors
  name no subject (the whole run failed, or the planner could not resolve it) retries its original scope unchanged.
- **Params:** type, template and operator params of the original; the keys the bus wrote while it ran are left behind
  (`retry_service.RUNTIME_PARAMS`: `context_numbers`, the per-subject numbers the QA check allows, and an earlier
  `retry_of`) and `params.retry_of` = the id of the retried task (a retry of a retry points at the retried one).
  Template rules are merged again, so a rule added to the template since is picked up.
- **Lane:** by size, as `POST tasks/` without `lane` (one subject → `now`, more → `queue`); the batch key is not kept.
- **Refused:** 404 unknown task · 409 `NOT_RETRYABLE` while the task is `open` / `in_progress`, or when it ended
  `done` / `cancelled` with no failed subject · 409 `ALREADY_RUNNING` when every subject is in flight again (a double
  click) · 400 for a workflow step (`params.run`, or a `run:<uid>` subject — regenerate it in its run) · whatever a
  spawn refuses (400: run cap, params no longer valid for the worker).

## Work queue: the worker pull API (plan 23)

A task is planned into small `WorkUnit`s (`ENRICHMENT_UNIT_SIZE`, default 5 subjects) after commit; internal and
external workers pull them the same way: **claim → (heartbeat) → complete | fail**. All endpoints are admin-only
(`JWTAuthentication` + staff), throttled by `WorkerQueueThrottle` (scope `enrichment_queue`, 300/min), area
`enrichment.proposals`.

| Method | Path | Body | Answer |
|---|---|---|---|
| POST | `workers/claim/` | `{worker, instance, version?, lane?}` | 200 unit · 204 nothing to do (also: worker paused, in-flight cap reached) · 404 unknown / inactive worker |
| POST | `workers/heartbeat/` | `{token}` | 200 `{lease_until}` · 409 `LEASE_LOST` |
| POST | `workers/complete/` | `{token, results: [{subject_ref, drafts: [{target_kind, target_locator, proposed_value, subject_label?}], error_code?, error_id?}]}` | 200 `{succeeded, failed, kept_edited, discarded}` · 400 (result names a subject outside the unit) · 409 `LEASE_LOST` |
| POST | `workers/fail/` | `{token, code, message, retryable=true}` | 200 `{unit_id, status, attempts, not_before}` · 409 `LEASE_LOST` |
| GET | `workers/status/` | — | list of `{key, label, mode, state (running\|paused), paused_reason, paused_at, online_instances, queued_units, oldest_queued_age_s, leased_units, failed_units_24h, nobody_taking}` |
| POST | `workers/{key}/resume/` | — | 200 status row · 404 |

Claim 200: `{unit_id, token, lease_until, attempt, task_id, type, params, scope_module, subject_refs, context}`
(`context` = the worker's `kind`, `version`, `system_prompt`, `model`, `options`). The lease lasts
`ENRICHMENT_LEASE_S` (300 s); a heartbeat extends it by the same time. A worker may hold
`ENRICHMENT_MAX_INFLIGHT_PER_WORKER` (2) leases. `complete` is idempotent per token: a repeat answers the stored
outcome. A subject missing from `results` counts as failed (`NO_RESULT`). Results go through the same intake as before
(supersede, keep-edited). After a cancel, `complete` answers `discarded: true` and stores nothing.

`fail`: retryable and attempts < `ENRICHMENT_MAX_ATTEMPTS` (3) → the unit is queued again after
`ENRICHMENT_RETRY_BACKOFF_S` × attempt; else it fails for good (task `counts.failed`, `errors`, a notification).
A budget error code (`ENRICHMENT_BUDGET_ERROR_CODES`) pauses the worker at once.

Task detail: `GET tasks/` and `GET tasks/{id}/` carry `units` (`{queued, leased, done, failed, cancelled}`);
the detail adds `failed_units` (`[{id, subject_refs, attempts, error: {code, message}, events}]`, last 5 events each).
`PATCH tasks/{id}/` stays for the CMS (cancel): queued units are cancelled, a leased one finishes and its results are
discarded.

**Deprecated for external workers:** the old loop (`GET tasks/?status=open` → `tasks/{id}/targets/` → `POST
proposals/` → `PATCH tasks/{id}/` done) still works but bypasses leases, retries and escalation. Use the pull API.

**Gap-mode tasks** are re-planned by the queue: when a gap task's units run out, page 1 is resolved again and only
subjects without a unit become new units; a task ends when nothing new appears.

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

The spawned task carries the rule's budget and key in `params`, next to the rule's `task_params`: `limit` (the rule's,
else `ENRICHMENT_SPAWN_DEFAULT_LIMIT`), `cooldown_days` (the rule's, else `ENRICHMENT_SPAWN_COOLDOWN_DAYS`) and
`spawn_rule` (the rule's `key`). They are bus-level keys: a worker's kind validation drops every param its kind does
not declare, these three survive it (`gap_params.carried_params`; `limit` and `cooldown_days` non-negative integers,
`spawn_rule` at most 64 characters, else 400). So a run plans at most `limit` subjects (`counts.proposed` reaching it
closes the task), a target rejected or applied within `cooldown_days` stays out, and the task names its rule. A
`gap`-scope task from `POST tasks/` takes `limit` and `cooldown_days` the same way; `spawn_rule` is a label, nothing
reads it.

## Intake idempotency

`POST /proposals/` is idempotent on `external_ref` (the worker's execution id): re-posting the same
`external_ref` returns the existing proposal instead of creating a duplicate. Intake also snapshots
the live value via `adapter.read_current` (never the worker's "before") and supersedes any older
`pending` proposal on the same `(target_module, subject_ref, target_kind, locator_hash)` target.
The internal runner never supersedes a pending proposal the operator already worked on (a revision after the
first: edit, correction, restore): it drops its own draft for that target and counts it in `counts.kept_edited`.

Intake is throttled (`enrichment_intake` scope, `100/min` fallback) — it's the high-volume,
worker-facing path. The other endpoints (operator review) are not throttled.

## Review filters

`GET /proposals/` filters server-side: `status` (default `pending`; `all` = every status), `target_module`, `target_kind`,
`batch_id`, `source`, `confidence_min`, `search` (part of `subject_label` or `subject_ref`), `subject_ref` (exact). `ordering` is validated
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

## Failure codes

An `errors` entry is `{subject_ref, code, error_id, message?, exception?}`. `error_id` finds the trace in the `process`
log. `message` is the toolbox's own capped reason; for a failure with no HTTP answer it is the client's fixed text
(`Connection failed: ConnectError`). `exception` appears only with `INTERNAL_ERROR` and is the exception class name
(`FileNotFoundError`), never its text. A toolbox error keeps the toolbox's code (`BUDGET_EXCEEDED`, `PROVIDER_ERROR`,
...), or `TOOLBOX_HTTP_<status>` when it sent none. Codes a media run (`video`, `image`) can fail a subject with:

| Code | Meaning |
|---|---|
| `PICTURE_NOT_FOUND` | the chosen `picture_ref` is not one of the subject's pictures |
| `NO_PICTURE` | `image` kind: the subject has no main picture |
| `PICTURE_UNREADABLE` | the picture's file cannot be read - gone from the worker's disk, or not an image |
| `UPSTREAM_TIMEOUT` | the toolbox did not answer in time (a text call gets the same code) |
| `TOOLBOX_HTTP_0` | the toolbox cannot be reached - refused, DNS, connection reset (a text call gets the same code) |
| `AI_TOOLBOX_NOT_CONFIGURED` | the toolbox has no URL, API key or channel (`ENRICHMENT_VIDEO_TOOLBOX_*` / `AI_TOOLBOX_*`) |
| `TIMEOUT` | the media job was not done within `ENRICHMENT_VIDEO_TIMEOUT_S` / `ENRICHMENT_IMAGE_TIMEOUT_S` |
| `IMAGE_BLOCKED`, `VIDEO_BLOCKED`, `PROVIDER_ERROR`, ... | the toolbox job failed with its own code |
| `INTERNAL_ERROR` | anything else; `exception` names the class |

## Costs (contract A4 § 1)

Post factum only — no estimates, no markup. Task responses carry `cost_usd` (sum of the known toolbox
costs, null = none known), `cost_breakdown` (`{completion, translate, seo, media}`) and `cost_complete`
(false once a call reported no cost, failed after its request left, or ran before the ledger); proposal and revision responses carry `cost_usd` (null = unknown).
Corrections and `variants` are charged to the proposal's task. `toolbox_calls.cost_of` is the single
mapping from today's toolbox fields (completion `cost`, translator `total_cost_usd`, media job `cost`, SEO
`cost`, also on SEO cache hits = 0), normalised to 8 decimal places; NaN, negative or out-of-range values count as unknown. Completion calls send `tags: ["enrichment.<kind>[.<template>]", "task:<id>"]` — the selector
first (the toolbox fake provider picks fixtures by it), then the task for usage attribution.

## AI usage this month

`GET usage/month/` → `{period, tokens, cost_usd, cost_since, currency}` for the current UTC month (the tracker's
month), area `enrichment.proposals`, cached 60 s per month (`enrichment:ai-usage:<YYYY-MM>`; failures are not cached).
`toolbox_calls.tracker_month` reads two toolbox tracker endpoints of the instance client (`AI_TOOLBOX_CHANNEL`), one
attempt each with a 5 s timeout:

- `usage/?year=&month=` → `tokens` = input + output of the rows counted in LLM tokens: tool `completion`, and
  `translator` unless the provider is `deepl` (characters). SEO (requests) and media (seconds) are left out.
- `billing/client/?year_month=&granularity=none` → `cost_usd` = `totals.amount_due_usd`: the billed amount, provider
  cost plus the toolbox markup, rounded to cents by the toolbox. `null` while the toolbox has billing off for the
  client (404). `cost_since` = the billing start when it falls inside the month (the cost covers only from then).

Errors: `AI_TOOLBOX_DISABLED` / `AI_TOOLBOX_NOT_CONFIGURED` (503) as elsewhere; any other toolbox failure (down,
timeout, auth, a body that does not match) → 503 `AI_USAGE_UNAVAILABLE`, logged with the error type and code. The
toolbox client needs `django_ai_tracker` among its tools (`bind_client_tools`, toolbox ≥ 0.4.0) and, for the cost,
billing enabled on its channel; a key restricted to scopes needs `tracker.usage.read` and `tracker.billing.read`.

## Accept / drift (D2)

`POST /proposals/{id}/accept/` routes through the drift-aware apply: it re-reads the live value and,
if it diverged from the snapshot, flips the proposal to `drifted` (no write) for the operator to
re-confirm. No drift → the adapter writes and the proposal becomes `applied`. `bulk-accept/` does the
same over a filtered `pending` set asynchronously (Celery); `bulk-reject/` is a synchronous
`.update()`.

**Apply mode (plan 25).** A proposal lists `apply_modes` — the ways its adapter can write it (optional adapter
extra `apply_modes(*, target_kind, target_locator)`); `[]` = one fixed way. For a picture made from a product
picture PIM offers `["add", "replace"]`. The accept body takes an optional `mode` (`add` | `replace`, default
`add`); a mode the proposal does not offer → 400 and nothing is written. The choice is stored in `apply_mode`
(blank when no choice was offered) before the adapter's `apply`, and `revert` reads it. `bulk-accept/` never
passes a mode, so a bulk accept always adds — replacing an original needs a single, conscious accept.

## Errors

All errors use the v2 envelope from `django-utils`:

```json
{"error": "VALIDATION_ERROR", "message": "...", "debug_id": "f7a2c3b8", "details": []}
```

`VALIDATION_ERROR` (400, Pydantic or service `ValueError`), `NOT_FOUND` (404, unknown id),
`ANSWERS_MISSING` / `COVER_NOT_CHOSEN` (400, approving a questions step without its answers / a cover without a pick),
`AUTHENTICATION_REQUIRED` (401), `PERMISSION_DENIED` (403, non-admin), `ALREADY_RUNNING` (409, `POST tasks/` and
`tasks/{id}/retry/` when every subject is already in flight — § Run lanes), `NOT_RETRYABLE` (409, `tasks/{id}/retry/` — § Retry a task), `RATE_LIMITED` (429, intake throttle). Internal errors never leak `str(exc)` — the handler returns `INTERNAL_ERROR` + a logged
`debug_id`.

## OpenAPI

Every action is `@extend_schema`-annotated (tags `Enrichment Tasks` / `Enrichment Proposals`), so the
schema generates without warnings. Browse it at `/api/docs/` (Swagger) or `/api/schema/`.
