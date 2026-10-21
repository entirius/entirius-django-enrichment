# Changelog

## Unreleased
- `POST tasks/{id}/retry/` (empty body): runs the failed part of a finished task again as a new task through the
  `POST tasks/` spawn path (params validation, lane, in-flight dedupe, planning, `requested_by`), answering like it
  (201, `TaskSpawnResponse`). The scope is the distinct `subject_ref`s of the task's `errors` as a list scope; a failed
  task naming none retries its whole scope. Same type, template and params, without `context_numbers`, plus
  `params.retry_of`. 409 `NOT_RETRYABLE` while it runs or when nothing failed, 409 `ALREADY_RUNNING` when it is in
  flight again, 400 for a workflow step. Access area `enrichment.proposals` (write), `enrichment_spawn` throttle.
- Kind `sections`: a long artifact written section by section from the approved outline (`outline_step`, default
  `outline`): one completion per numbered outline heading, each seeing every section written before it, then one
  for the lead and the closing; every section starts with `## <its outline title>`; a regeneration hands each
  section only its own part of the previous version and the operator instruction (never a trailing "write a new
  version" that would invite a whole article); an outline without numbered headings is written in one call (logged).
  An `artifact` step may run a `generate` or a `sections` worker (`STEP_OUTPUT_KINDS` maps an output to a tuple of
  kinds).
- Lease keepalive: every toolbox call heartbeats the running unit (at most once per 60 s), so a subject with many
  paid calls no longer outlives its lease and gets paid twice; a lease lost inside a subject stops the consumer
  instead of counting as a failed subject.
- AI media runs (`video`, `image`) fail with a specific code instead of `INTERNAL_ERROR`: `PICTURE_UNREADABLE` when the
  picture's file cannot be read (an `OSError` from the adapter's `main_picture` / `picture`: gone from the worker's
  disk, not an image); `UPSTREAM_TIMEOUT` and the connection error (`TOOLBOX_HTTP_0`) when the toolbox media API times
  out or cannot be reached - the same codes a text call gets, the media calls now raise the typed toolbox errors
  `ToolboxClient` raises; `AI_TOOLBOX_NOT_CONFIGURED` (was a `ValueError`) when the media toolbox has no URL, key or
  channel. A failure with no HTTP answer carries the client's own text as `message` (`Connection failed: ConnectError`)
  instead of "HTTP 0 -: no message".
- A subject that still fails with `INTERNAL_ERROR` stores the exception class name in its `errors[]` entry
  (`exception`, e.g. `FileNotFoundError`; absent for every other code, never the exception text), so the failure can be
  told apart without the container log. `errors` is documented in the OpenAPI schema.
- `seo_brief` keeps its search evidence (seed volumes, keyword ideas, SERP with people also ask) in the remembered
  brief; `params.seo_detail: full` adds it to the prompt block (the short brief stays the default and the memory
  text). Steps of one run reuse the research at cost 0.
- `seo_brief` seeds of a workflow step (an article) come from a topic prompt: 2-4 word head terms instead of
  question-like phrases, which have no search volume and left `keyword-ideas/` nothing to expand. Items keep the
  item prompt.
- Questions step: `min_answers` (default 1, 0 = off) — approval needs at least that many answers, optional
  questions included (capped at the number of questions); 400 `ANSWERS_MISSING` names the minimum in `message`
  and lists the unanswered questions as `details[].issue` `MIN_ANSWERS` (a missing required answer stays `REQUIRED`).
- Workflow step prompts carry the brief's language as an explicit instruction.
- QA checks `{"data"}` values too (metadata, search phrases): `LIMIT` warnings against the step's `json_schema`
  (`maxLength` / `minLength` / `maxItems`, which the provider only sees as hints), then the rule and number checks on
  every text field; warnings carry `path` (`faq[2].answer`, null for a text value) — a new nullable field of
  `QaWarning` in the API.
- QA flags a profile fact citation that is not in the facts registry: `FACT_NOT_IN_PROFILE` for every
  `(profile fact: <id>)` whose `<id>` is not an `id` of the merged profile's `facts` (once per distinct id, span on the
  citation; with an empty registry every citation is flagged). The model had invented a "From the company profile"
  section for an empty profile and downstream steps treated it as ground truth. The format is now a contract between
  workflow prompts and QA (`docs/api.md` § QA checks).
- A JSON step's strings lose the escapes a model wrote twice: a literal `\"` or `\n` (the CMS showed the backslashes,
  e.g. in the search phrases' SERP notes) becomes a quote or a newline in every string of the `{"data"}` value before
  intake. `\\"`, other backslash sequences, keys and `{"text"}` values are left as they are
  (`services/json_unescape.py`, applied in kind `generate`; regenerations pass through it too).
- Spawn rules: a rule's run keeps its `limit`, `cooldown_days` and key. A worker's kind validation dropped them from
  the task's params (`limit`, `cooldown_days`, `spawn_rule` are not params of any kind), so "Run now" planned every gap
  on the rule instead of its `limit`, the rule's `cooldown_days` gave way to the module default and the task did not
  name the rule that spawned it. The three keys now survive the validation (`services/gap_params.py`, carried like
  `params.augment` and `qa_check`) and are checked on the way in: `limit` and `cooldown_days` non-negative integers,
  `spawn_rule` at most 64 characters, else 400. A `gap`-scope task from `POST tasks/` takes `limit` and `cooldown_days`
  the same way.

## 2.3.0rc3 — 2026-10-17
- A toolbox image or video job that fails (`IMAGE_BLOCKED`, `VIDEO_BLOCKED`, `PROVIDER_ERROR`, ...) fails the subject
  with the toolbox's code instead of `INTERNAL_ERROR`, so the CMS names the reason (e.g. the provider's safety filter).

## 2.3.0rc2 — 2026-10-17
- A task from a template whose run picks another image `operation` drops the template's `preset`: the toolbox
  refused a preset of another operation with `PRESET_NOT_FOUND` (e.g. "Enhance photo" run as a packshot); without a
  preset it takes the default of the chosen operation.

## 2.3.0rc1 — 2026-10-16
- Proposal status changes lock the row and refuse a status that moved meanwhile: a concurrent apply and reject
  can no longer both win (the loser gets a 400 and its write rolls back).
- SEO brief: when every proposed seed phrase is over the toolbox keyword limit (80 characters), the phrases are cut
  at the last word break within it instead of failing the subject with `SEO_BRIEF_NO_SEEDS`; seeds within the limit
  still win and the overlong ones are dropped next to them.
- Image processing (plan 25): kind `image` — params `operation` (packshot | lifestyle | enhance | instruct), `preset`,
  `prompt` (≤ 1000, required for a run of `instruct`; a template may leave it to the run), `picture_ref` (blank = the
  main picture from `list_pictures`), `n` 1–4. `toolbox_calls.generate_image` runs the toolbox image job (submit,
  poll up to `ENRICHMENT_IMAGE_TIMEOUT_S` = 300, download every output; costed like video); each output becomes one
  picture proposal with locator `{channel, source_picture_ref, operation, variant}`.
- Accept mode: `POST proposals/{id}/accept/` takes an optional `mode` (`add` | `replace`, default `add`); proposals
  carry `apply_modes` (from the optional adapter extra `apply_modes`) and `apply_mode` (migration `0006`). A mode
  that is not offered → 400. Bulk accept always adds. The adapter reads `proposal.apply_mode` in `apply` / `revert`
  and may return `{"ref"}` (kept in `result_ref`) for an object the apply created.
- Video from a chosen picture (plan 24): `GET subjects/pictures/?module=&channel=&ref=` (area `enrichment.proposals`,
  throttle `enrichment_lookup`) → `[{ref, label, url, is_main}]`, backed by the optional adapter extra
  `list_pictures(*, subject_ref, channel)` (absent → 404 `NOT_SUPPORTED`). The `video` kind takes an optional
  `params.picture_ref` and reads that picture through the optional extra `picture(*, subject_ref, channel, picture_ref)`;
  a picture that is not the subject's fails it with `PICTURE_NOT_FOUND`. Without the ref it animates the main picture.
- Video job calls report the toolbox's own error code and message (e.g. `PRESET_NOT_FOUND`) instead of an
  unexpected error.
- `POST subjects/resolve/` (area `enrichment.proposals`, throttle `enrichment_lookup` 60/min): `{module, channel, refs}`
  (≤ 500) → `{known: [{ref, label}], unknown: [ref]}` — the CMS checks pasted SKUs before a run. Backed by the new
  optional adapter extra `subject_labels(*, refs, channel)` (absent → 404 `NOT_SUPPORTED`); the test fake adapter
  serves it from `set_subjects`.
- Completions send `max_tokens` (`ENRICHMENT_COMPLETION_MAX_TOKENS`, default 8192): the toolbox default of 1024
  truncated article drafts, answered as 502 `PROVIDER_ERROR`.
- `seo_brief`: a failed SERP call leaves that phrase out (cost marked unknown) instead of failing the subject.
- Transient toolbox failures (`TOOLBOX_HTTP_502/503/504`, `TIMEOUT`) are retried in place once per subject
  (`ENRICHMENT_TRANSIENT_RETRIES`, `ENRICHMENT_TRANSIENT_ERROR_CODES`) before the subject counts as failed.
- Provider-portable completion schema: `toolbox_calls.complete` sends the schema without value limits (`minLength`,
  `maxLength`, `minItems`, `maxItems`, `minimum`, `maximum`) and format keywords (`pattern`, `format`, `uniqueItems`, ...);
  limits stay as `(max N chars)` hints in `description`. A model answer that broke a limit made the toolbox answer 502
  on real models (Haiku). Failed toolbox calls now put `HTTP <status> <code>: <message>` in the task error `message`
  and the log.
- Work queue (plan 23): tasks are split into small `WorkUnit`s (migration 0014; `WorkerInstance`, `Worker.paused_reason`
  / `paused_at`) that internal and external workers pull the same way — `POST workers/claim|heartbeat|complete|fail/`
  with leases, retries with backoff, idempotent complete and 409 `LEASE_LOST`; `GET workers/status/`,
  `POST workers/{key}/resume/`; task responses carry `units` (and `failed_units` on retrieve). Celery:
  `enrichment.plan_task`, `enrichment.consume`, `enrichment.reap` (host beat, 60 s); `enrichment.run_task` is a shim.
  Escalation (pause after repeated failures or a budget error, failed units, nobody taking work) goes through
  `django_notifications` when installed, log only otherwise. Logger `enrichment.queue`. Settings: see AGENTS.md.
  `dispatch_if_internal` becomes `worker_service.dispatch`; the old open-tasks + PATCH loop is deprecated.
- Run guards: `ENRICHMENT_QUEUE_MAX_SUBJECTS` (500; list scope over it → 400) and `ENRICHMENT_TASK_MAX_COST_USD`
  (5.00; a task at the limit stops with the plain error "Stopped: this run reached its cost limit").
- Keep-edited fix: the operator-edited check runs before the paid call and again inside the supersede transaction
  (`proposal_service.KeptEdited`), so an edit saved in between is never superseded.

- `GET usage/month/` (area `enrichment.proposals`): this month's AI usage of the instance's toolbox client for the CMS
  header counter — `{period, tokens, cost_usd, cost_since, currency}`. Tokens from the toolbox tracker `usage/`,
  cost = the billed amount with the toolbox markup from `billing/client/` (null while billing is off). Cached 60 s;
  a toolbox that is down answers 503 `AI_USAGE_UNAVAILABLE`, never a 500. Needs toolbox ≥ 0.4.0 with
  `django_ai_tracker` among the client's tools. `toolbox_calls.tracker_month`, `ai_usage_service`.

- Run lanes (plan 22): `EnrichmentTask.lane` (`now` | `queue`, migration 0013). The internal runner goes to
  `ENRICHMENT_INTERACTIVE_QUEUE` (`enrichment_interactive`) or `ENRICHMENT_BULK_QUEUE` (`enrichment_bulk`) by lane
  instead of the default `celery` queue; workflow steps and batch corrections always use the interactive queue — a
  host must now run workers on both queues (bulk with concurrency 1). `POST tasks/` takes `lane` (omitted = one
  subject → `now`, more → `queue`); `now` above `ENRICHMENT_RUN_NOW_MAX_SUBJECTS` (20) → 400. Subjects that already
  have the same run in flight (type, template, channel, fields, languages) are skipped and listed in
  `skipped_in_flight`; all skipped → 409 `ALREADY_RUNNING`. Task responses carry `lane` and `queued_ahead`.

- `GET proposals/?status=all` lists every status (the CMS Review "All" chip); no `status` keeps the default `pending`.

- Review fixes: a channel overlay adds to the instance `always_rules`, `rules` and `facts` (joined by id, the overlay
  wins on the same id) instead of replacing them — promoting a memory suggestion into a channel no longer hides the
  instance rules and facts from prompts, the QA number check and `status`. Workflow steps pass `brief.language` to
  augmentations (`seo_brief` researched the default market). `POST proposals/{id}/rules/promote/` moves to the area
  `enrichment.profile` (a worker or template rule is an instance-wide instruction). `POST tasks/{id}/revise/` runs one
  batch per task at a time and at most `ENRICHMENT_REVISE_BATCH_MAX` (200) proposals.

- Augmentations (contract A4 § 5): registry `register_augmentation(key, fn)` → `AugmentResult{block, cost,
  memory_writes}`; enabled per task with `params.augment` (validated against the registry, unknown key → 400;
  templates may preset it, a template with an unknown key is refused); kinds `write`, `pick` and
  the workflow step kinds append the blocks as fenced data sections. First augmentation `seo_brief`: reuses a fresh
  memory brief (subject, then category; cost 0), else seed phrases (completion) → toolbox SEO `search-volume/`,
  `keyword-ideas/` and `serp/` for the top 3 seeds (`toolbox_calls.seo`, charged as `seo` in the cost ledger) →
  intent-first evaluation (completion) → `{primary, secondary, intent, serp_notes, gaps, search_volume}`, remembered
  for 90 days under the category (`params.seo_scope`, default `category` for kind `write`) or the subject. Failures
  fail the subject with `SEO_BRIEF_<code>`. `memory_service.fresh` looks up one entry (subject, then category).
  Briefs are per language: `language_code` on every SEO call, `language` stored in the brief and required for reuse,
  `write` runs augmentations per subject × language. The `memory` block leaves out the kinds the task's
  augmentations supply (no duplicate brief). Answers and SEO data are validated with Pydantic models; only toolbox
  and validation errors map to `SEO_BRIEF_<code>`, other errors reach the runner as `INTERNAL_ERROR`.

- QA checks (contract A4 § 4): every revision of a text value is checked deterministically (`qa_service`) and carries
  `warnings` `[{code, message, span}]`, copied onto the proposal — `rule` (active profile `rules` with flags,
  `language.spellings`; a pattern that does not compile disables only its rule and logs) and `NUMBER_NOT_IN_FACTS`
  (numbers compared by value — integers, comma/dot decimals, thousands groups, percentages, ranges — against the facts
  registry, the task's source params (`prompt`, `brief`, `inputs`) and the subject's source-context numbers the kinds
  store in `params.context_numbers`). Quick actions `check_standard` (one completion against the profile checklists →
  `checklist` warnings, no revision; stored row-locked, 422 when the value changed meanwhile) and `fix_warnings` (a
  fixed instruction, the warnings as a fenced data block); not available in batch corrections. API: `warnings` in
  proposal and revision responses, `POST proposals/{id}/check/` (deterministic re-run), `POST
  proposals/{id}/facts/suggest/` (memory `fact_suggestion`, "add to facts"). Migration `0012_qa_warnings`.
- Company profile (contract A4 § 2): `Profile` (instance row + channel overlays, `data` validated by
  `schemas/profile_schema.json`, `version`) and `ProfileVersion` snapshots on every save; restore = new version.
  `profile_service.context_for` renders the merged profile for prompts — sections by `routing` (task type, kind,
  defaults), order facts → brand → rest, `always_rules` always, operator text as instructions (system message),
  facts as a data table, 12 000-character cap with a truncation note (the conflict note and `always_rules` are
  always kept, facts and other sections are cut at whole lines); `status` reports section minimums.
  Kinds `write` / `pick` / `generate` / `questions` and corrections use it; `EnrichmentTask.profile_versions` and
  `ProposalRevision.profile_versions` record the versions behind a prompt. Migration `0011_profile_memory`.
- Memory (contract A4 § 3): `MemoryEntry` (`seo_brief | answer | rule_suggestion | fact_suggestion`, subject or
  general, expiry). `memory_service.recall` (subject, then its category via the optional adapter extra
  `category_of`; newest first; budgeted) feeds the same prompts. Approved workflow questions write `answer`
  entries; rule promotion gains target `profile` (writes a `rule_suggestion`); `memory_promotion.promote` moves a
  suggestion into the profile (`always_rules`, `rules` or `facts`) as a new profile version.
- API: `GET/PUT profile/?channel=` (schema errors → 400 per `data.<path>`), `GET profile/versions/`,
  `POST profile/versions/{n}/restore/`, `GET profile/status/`, `GET profile/schema/`; `GET memory/`,
  `PATCH/DELETE memory/{id}/`, `POST memory/{id}/promote/`; task and revision responses add `profile_versions`.
  Profile and memory routes use their own access area `enrichment.profile` (narrower than `enrichment.proposals`).

- Workflows (contract A4 § 6): `WorkflowDefinition` (steps `{key, label, template, needs, gate review|auto,
  output artifact|questions|document}`, `inputs_schema`; `clean()` validates the graph, templates, kinds and
  schema) and `WorkflowRun` (`uid`, frozen `steps_state`, status `running|waiting|done|failed|cancelled`,
  `subject_ref`, `cost_usd` + `cost_complete` summed from the run's tasks) — migration `0010_workflows`,
  Django admin. `workflow_service`: start (brief validated against `inputs_schema`), steps spawned as template
  tasks on `run:<uid>` with frozen params, runner completion hook, gates (auto approves at once), approve =
  accept the step's proposal → next steps spawn, regenerate with an instruction, cancel. Completion calls of a
  step are tagged `run:<uid>`.
- Workflow hardening: approving a step's proposal fails closed (400, adapter write rolled back) when the run
  is closed or the proposal is no longer the step's current output — `apply_service` locks the run before
  writing (`workflow_service.guard_apply`). Regenerate also recovers a step stuck `running` on a closed task.
  `clean()` refuses a step template whose worker is inactive or external, and an `artifact`/`questions` step
  whose worker does not write to the `enrichment` adapter.
- Built-in adapter `"enrichment"` for workflow artifacts (`target_kind="artifact"`, locator `{"step"}`, values
  `{"text"}` / `{"data"}` / `{"questions", "answers"}`; revert refused). `ContentProposal.result_ref`: an
  adapter's `apply` may return `{"subject_ref"}` (documents); also set as the run's `subject_ref`.
- Kinds `generate` (markdown / text / json with an optional object schema), `questions` (approval needs the
  required answers → 400 `ANSWERS_MISSING`) and `compose` (no AI: article markdown + meta → document value
  `{language, title, slug, body_markdown, meta, faq}`). Optional seams (`kinds/seams.py`) for the company
  profile, memory recall / answer writes and augmentations.
- API: `GET workflows/`, `GET/POST runs/`, `GET runs/{uid}/`, `POST runs/{uid}/steps/{key}/regenerate/`,
  `POST runs/{uid}/cancel/`; proposal responses add `result_ref`. Dependency `jsonschema`.
- Cost ledger (contract A4 § 1), post factum only: `toolbox_calls` returns `Costed(value, cost)` for completion,
  translation and media jobs, the cost read by one mapping (`cost_of`: completion `cost`, translator
  `total_cost_usd`, media job `cost`, SEO `cost`); `cost_service.charge` accumulates it row-locked on
  `EnrichmentTask.cost_usd` + `cost_breakdown` + `cost_complete`; `ContentProposal.cost_usd` (even split per
  call) and `ProposalRevision.cost_usd` (migration `0008_cost_ledger`). An unknown cost stays null, never 0.
  Completion tags `["enrichment.<kind>[.<template>]", "task:<id>"]`. API: costs on task, proposal and revision
  responses. `cost_of` normalises to the ledger's 8 decimal places and treats NaN, negative or out-of-range
  values as unknown; a call that fails after its request left (completion, translation, a video job's
  polling) charges an unknown cost, a failed video job its reported cost. Migration
  `0009_pre_ledger_costs_unknown` marks tasks that ran before the ledger `cost_complete=false`.
- Generic kinds (contract A3 § 3): `write` (several text fields, one completion per subject × language,
  `maxLength` from the catalog, html sanitised, `languages: ["*"]`) and `pick` (select/multiselect enums, one
  completion per subject); `translate` takes `params.fields`. Adapter extras with fallbacks in `kinds/extras.py`;
  `SubjectError(code)` records codes such as `FIELD_TYPE` in `task.errors`. The item context is fenced so it
  cannot close its data block; catalog and channel languages are memoised per task run. New optional extra
  `field_key(*, locator)` (inverse of `make_locator`) gives corrections the field meta without the bus reading
  adapter-specific locator keys. Rule limits moved to the leaf `services/rule_limits.py`.
- Templates (contract A3 § 4): `TaskTemplate` model (migration `0007_task_template`, Django admin),
  `template_service`, API `GET/POST templates/`, `GET/PATCH/DELETE templates/{key}/`; `POST tasks/` accepts
  `template` (request params win, rules concatenated, `params.template` kept). Rule promotion to the template
  and template rules in correction prompts.
- Field catalog API (contract A3 § 2): `GET fields/` (404 `NOT_SUPPORTED` without `describe_fields`) and
  `GET languages/`.
- Adapter conformance kit `django_enrichment.testing.adapter_conformance` (read/apply/revert/drift, catalog
  shape, `make_locator`, document create), proven on the fake adapter.
- Revisions (contract A3 § 1): `ProposalRevision` model and `ContentProposal.rules` (migration
  `0006_proposal_revisions`); intake records revision 1, `PATCH proposals/{id}/` records a `user` revision
  (pending and drifted). Services `revision_service` (invariant owner), `revise_service` (one stateless
  toolbox completion per correction: edits / full value / option schemas, all-or-nothing edits, selection
  scope, quick actions, `variants`), `rules_service` (limits, promotion to the worker), `revise_batch_service`
  (Celery `enrichment.revise_task`, default queue; per-proposal codes `NOT_REVISABLE` /
  `REVISION_NOT_APPLICABLE` / toolbox codes, field catalog looked up once per run). Correction prompts fence
  field meta, current value and selection as data blocks that the data cannot close. Writes re-check status and (AI corrections) the base
  value under the row lock; a selection-scoped answer may not change text outside the selection (422).
- API: `GET proposals/{id}/revisions/`, `POST proposals/{id}/revise/` (422 `REVISION_NOT_APPLICABLE`),
  `POST proposals/{id}/revisions/{n}/restore/`, `PUT proposals/{id}/rules/`, `POST proposals/{id}/rules/promote/`,
  `POST tasks/{id}/revise/`, `GET proposals/?task=`; proposal responses add `rules`.
- AI workers: `Worker` model (Django admin only, migration `0004_worker`), kind registry (`register_kind`) with the
  base kinds `translate` and `field`, toolbox calls on `django_utils.toolbox.ToolboxClient`, and an internal Celery
  runner (`enrichment.run_task`) dispatched on commit when an active internal Worker owns the task type.
- API: `GET workers/`, params validation on `POST tasks/` when a Worker owns the type, `PATCH proposals/{id}/`
  (pending only).
- Product video (Amendment A1): `intake_media(target_kind="video")` accepts mp4 (magic bytes,
  `ENRICHMENT_MAX_VIDEO_BYTES`); kind `video` (adapter `main_picture` → toolbox media job → one staged video
  proposal per subject), polling up to `ENRICHMENT_VIDEO_TIMEOUT_S`; `ENRICHMENT_VIDEO_TOOLBOX_URL/_API_KEY/_CHANNEL`
  (default `AI_TOOLBOX_*`, POC). `ProposalDraft.media_file` routes a draft to `intake_media`.
- API: per-product reads for the CMS product page — `GET proposals/?subject_ref=`, `GET tasks/?subject_ref=`
  (list-mode refs) `&target_module=`, `tasks/?status=` takes a comma list. `task_service.list_tasks` takes a
  `TaskFilters` dataclass.
- Failure reasons: `EnrichmentTask.errors` (migration `0005_task_errors`) — the runner records
  `{subject_ref, code, error_id}` per failed subject (toolbox error code via `toolbox_calls.failure_code`,
  never exception text, capped at 20). Task responses add `errors` and `proposal_counts`.
- Dependencies: `httpx` (video job calls), `entirius-django-utils>=2.1.0` (toolbox client), `nh3` (HTML sanitising); `respx` in the `test` extra.

## 2.2.0 — 2026-10-12

- Access: the module declares its own access areas on its AppConfig and its admin views (copied from the
  entirius-django-access defaults; behaviour unchanged).

## 2.1.0 — 2026-08-27

- Optional `on_reject` adapter hook on both reject paths (`adapters/base.py`,
  `proposal_service`). A proposing module can now learn that a human turned its
  proposal down, instead of re-proposing the same pair once the cooldown lapses.
  The hook is fetched with `getattr(adapter, "on_reject", None)`, so adapters that
  do not implement it are unaffected and `bulk_reject` stays a single UPDATE for
  every module without one.
  `django_atlas.services.enrichment_adapter` implements it for the
  `duplicate_in_pim` acceptance queue, feeding `dedup_log.rejected_pairs`.

## 2.0.0 — 2026-07-11

- Initial public release: the horizontal content-quality bus —
  accept-before-write enrichment for product content. Models, service layer,
  the PIM adapter, spawn rules and surfaces, media proposals, scale/undo,
  and the v2 admin API.
- Migrations squashed into a single initial migration for the Entirius epoch.
