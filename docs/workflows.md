# Workflows, artifacts and the step kinds

Contract A4 § 6. A workflow turns a short brief into a finished document through ordered steps with
gates: ask the operator, draft, write meta, compose. Every step is an ordinary task of a template, every
step output is an ordinary proposal — reviewed, corrected (revisions), approved — so the whole review
toolbox works on workflow steps unchanged.

## Definition

`WorkflowDefinition` (Django admin or seed): `key`, `label`, `target_module` (adapter key the final
document goes to), `inputs_schema` (JSON Schema of the brief, `"type": "object"`; `{}` = any object),
`steps`, `active`.

```json
[{"key": "ask",     "label": "Questions", "template": "article.ask",     "needs": [],                 "gate": "review", "output": "questions"},
 {"key": "draft",   "label": "Draft",     "template": "article.draft",   "needs": ["ask"],            "gate": "review", "output": "artifact"},
 {"key": "meta",    "label": "Meta",      "template": "article.meta",    "needs": ["draft"],          "gate": "auto",   "output": "artifact"},
 {"key": "compose", "label": "Document",  "template": "article.compose", "needs": ["draft", "meta"], "gate": "review", "output": "document"}]
```

`clean()` (`workflow_spec.definition_errors`) refuses: no steps, a malformed step, a duplicate key, `needs`
naming a step that is not earlier (so a definition is acyclic), a `questions` or `image` step without the `review`
gate, an unknown template, a template whose worker is inactive or not `internal`, a template whose worker kind does not make the output (`artifact` → `generate` or `sections`,
`questions` → `questions`, `document` → `compose`, `image` → `cover`), a `document` step whose worker writes to another
module than `target_module`, an `artifact`/`questions`/`image` step whose worker does not write to the built-in
`enrichment` adapter, and an invalid inputs schema.

An inputs-schema property with `x-picture-of: <adapter key>` takes `{channel, ref}` of that module's subject; `start`
refuses (400) a subject that does not exist or has no main picture, so the brief fails before any paid step. A
property with `x-steps: [step keys]` reaches only those steps' briefs (`workflow_steps.brief_for`): the cover's hints,
text and product stay out of the research and the writing.

## Run

`WorkflowRun`: `uid`, `definition`, `channel`, `inputs` (the brief), `subject_ref` (what the final step
created), `status` `running | waiting | done | failed | cancelled`, `steps_state`, `cost_usd`,
`cost_complete`, `created_by`.

`steps_state` is an ordered list: the definition's steps copied at start (editing a definition never
changes a running run), each with `status` (`waiting | running | review | approved | failed`), `task_id`
and `proposal_id` (the step output). The run status is derived from its steps: anything running →
`running`, else a failure → `failed`, else something in review → `waiting`; all approved → `done`.

| Action | What happens |
|---|---|
| start | brief validated against `inputs_schema`; every step without `needs` is spawned |
| spawn | a task of the step's template on the subject `run:<uid>` (scope `{"mode": "list", "module": "enrichment"}`) with frozen params: `run`, `step`, `channel`, `brief`, `inputs` (approved values of the needed steps), `instruction`, `previous` |
| task finished | runner hook `workflow_service.on_task_finished`: the task's pending proposal → step `review`; no proposal or a failed task → step `failed`; `auto` gate → the proposal is applied at once (a refused apply → `failed`) |
| approve | accept the step's proposal (`POST proposals/{id}/accept/`, bulk accept too). `apply_service` calls `on_proposal_applied` for every `run:` proposal in the same transaction: step `approved`, `result_ref` → `run.subject_ref`, answers → memory seam, ready steps spawn (a failing spawn rolls the approval back). `apply_service` first locks the run (`guard_apply`, same lock order as cancel/regenerate); a closed run or a proposal that is no longer the step's current output (superseded, or the step already approved) → 400 and the adapter write rolls back |
| regenerate | a step in `review` or `failed`, or `running` on a task already closed (runner skipped it, cancelled via the tasks API, failed hook): its pending proposal is superseded, a new task gets the same params plus `previous` (that value) and the operator's `instruction` |
| cancel | running step tasks → `cancelled`, the run's pending proposals rejected, run `cancelled` (final; a late runner hook changes nothing) |

Every state change takes the run's row lock. `cost_usd` sums the known costs of every task the run spawned
(regenerations included; `params.run` finds them), refreshed on every step event; `cost_complete` is false
once one of them has an unknown cost. Completion calls of a step are tagged `run:<uid>`.

## Artifacts — the built-in adapter `"enrichment"`

Intermediate outputs live only in their proposal: `target_module="enrichment"`, `target_kind="artifact"`,
`subject_ref="run:<uid>"`, locator `{"step"}`, value `{"text"}` | `{"data"}` | `{"questions", "answers"}`.
The adapter (`adapters/artifact.py`) is registered by the bus itself (a host entry with the same key wins):
`read_current` → `{}` (never drifts), `apply` checks a questions artifact's answers, `revert` refuses
(400 — regenerate instead), `resolve_targets` echoes the list refs.

## Document — `result_ref`

The `document` step's proposal goes to the definition's `target_module` (`target_kind="document"`). An
adapter's `apply` may return `{"subject_ref": <created ref>}`; the bus stores it in
`ContentProposal.result_ref` (and the run in `subject_ref`), and the adapter's `revert` reads it. The
conformance kit's `DocumentCreateConformance` checks the shape.

## Kinds

All step kinds take the step params (`kinds/step.py`: `channel, run, step, brief, inputs, prompt, rules,
instruction, previous, profile_sections`); templates preset `prompt` / `rules` / `profile_sections` (the profile
sections the step reads instead of its kind's routing — the profile's routing of the task type still wins) and the
kind's own keys.

| Kind | Own params | Call | Value |
|---|---|---|---|
| `generate` | `format: markdown \| text \| json`, `json_schema` (an object; json only) | one completion | `{"text"}`, or `{"data"}` shaped by the schema |
| `sections` | `outline_step` (`outline`) | one completion per numbered outline heading (`### N. <title>` + the lines under it), each with its outline block and every section written before it (`sections_so_far`), then one for the lead and the closing (`article_body` → `{lead, closing}`); a regeneration hands each section only its own part of the previous version (matched by `## ` title) and the operator instruction before its task, the lead/closing call the whole previous version; an outline without numbered headings → one completion (logged); the calls keep the unit's lease alive | `{"text"}` = lead, sections (each under `## <its outline title>`, whatever heading the model wrote), closing; cost = the sum of the calls |
| `questions` | `min_answers` (1; 0 = off) | one completion, schema `{questions: [{id, question, why, type: text\|choice, choices, optional}]}`; sees profile facts and remembered answers | `{"questions": [...], "answers": {}, "min_answers": n}` (ids made unique, `choice` without choices → `text`) |
| `compose` | `body_step` (`draft`), `meta_step` (`meta`), `language` (blank = `brief.language`), `cover_step` (blank = no cover) | none (cost 0) | `{language, title, slug, body_markdown, meta, faq}`; title = meta title, else the first `# heading`, else `brief.title`; slug slugified; `meta` = the meta data without title/slug/faq; with `cover_step` also `cover: {alt, file_name}` and the picked picture as the staged file |
| `cover` | `scenes_step` (`cover_brief`), `aspect_ratio` (`16:9`), `max_variants` (3), `product_field` (`cover_product`), `product_module` (`pim`), `text_flag` (`cover_text_wanted`), `text_field` (`cover_text`) | one toolbox image job per scene (`instruct`, one output) on the product's main picture or a blank canvas; the prompt = lead + the scene + code's tail (the scene's `must_show` as `Must show:`, never cut; profile `visual`, capped at 300 / 150 characters: `Style:` / `Never show:`, the brief's text or a ban on any text, `Change: <instruction>`), cut to 1000 characters at the scene; a failed scene is left out | `{variants: [{title, prompt, alt, must_show}], chosen: null, alt, cover_text}`, the pictures in one staged bundle (`?variant=k`); cost = the sum of the jobs |

**Answers.** The operator fills `answers` (`{question id: text}`) with a manual edit (`PATCH proposals/{id}/`)
and approves. With `min_answers` > 0 any answers count and no question is required on its own: fewer answers than
`min_answers` (capped at the number of questions) → 400 `ANSWERS_MISSING` with every unanswered question in `details`
as `issue` `MIN_ANSWERS` and the minimum in `message`. With `min_answers: 0` (or an artifact made before it) a
non-optional question without an answer → the same 400 (`details[].field` = the question ids, `issue` `REQUIRED`).
A `choice` answer outside its choices → 400.

**Cover pick.** The operator sets `chosen` (the variant index; the CMS also moves `alt` to that variant's unless the
operator wrote one) with `PATCH proposals/{id}/` and approves; no valid pick → 400 `COVER_NOT_CHOSEN`. The approved
artifact keeps its bundle for `compose`; the retention prune drops it.
An artifact without `min_answers` (made before it existed) keeps the old rule.

**Prompt.** System = worker system prompt + profile instructions. User = rules, the template prompt, a
data note, fenced data blocks (`brief`, `inputs`, `company_profile`, `memory` about `run:<uid>`, augmentation
blocks), the kind's task, the brief's `language` as an explicit instruction ("write all text in …"; none
without a language), then for a regeneration the fenced `previous_version` and the operator instruction.

## Seams for profile, memory and augmentations (`kinds/seams.py`)

Looked up when used; absent → the section is simply missing. The profile and memory services exist
(`docs/profile.md`), augmentations in `django_enrichment.augmentations` (`docs/api.md` § Augmentations):

| Seam | Callable |
|---|---|
| profile | `services.profile_service.context_for(*, channel, task_type, kind) -> (instructions, data)` |
| profile versions | `services.profile_service.version_ids(channel)`, `record_use(*, task, channel)` (stamps `task.profile_versions`) |
| memory recall | `services.memory_service.recall(*, channel, subject_module, subject_ref, kinds, budget_chars) -> str` |
| answers written | `services.memory_service.record_answers(*, run, step, value) -> None` (on questions approval) |
| augmentations | `augmentations.run_all(*, task, subject_ref, context) -> list[str]` (data blocks) |

## API (admin v2, JWT + IsAdminUser, area `enrichment.proposals`)

| Method | Path | Notes |
|---|---|---|
| `GET` | `workflows/` | active definitions `[{key, label, target_module, inputs_schema, steps}]` |
| `GET` | `runs/?status=&definition=` | paginated, newest first |
| `POST` | `runs/` | `{definition, inputs, channel}` → 201 run (first steps queued); 400 bad brief / unknown definition. Throttled (`enrichment_spawn`) |
| `GET` | `runs/{uid}/` | `{uid, definition, definition_label, channel, inputs, subject_ref, status, current_step, steps: [{key, label, template, needs, gate, output, status, task_id, proposal_id}], cost_usd, cost_complete, created_by_id, created_at, modified_at}` |
| `POST` | `runs/{uid}/steps/{key}/regenerate/` | `{instruction?}` → 200 run; 400 step not in review/failed (nor stuck on a closed task) or run closed; 404 unknown step. Throttled |
| `POST` | `runs/{uid}/cancel/` | → 200 run; 400 when already done/cancelled |

A step's output is read and edited through the proposal endpoints (`proposal_id`).
