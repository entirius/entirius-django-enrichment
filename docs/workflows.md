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
naming a step that is not earlier (so a definition is acyclic), a `questions` step without the `review`
gate, an unknown template, a template whose worker kind does not make the output (`artifact` → `generate`,
`questions` → `questions`, `document` → `compose`), a `document` step whose worker writes to another module
than `target_module`, and an invalid inputs schema.

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
| approve | accept the step's proposal (`POST proposals/{id}/accept/`, bulk accept too). `apply_service` calls `on_proposal_applied` for every `run:` proposal in the same transaction: step `approved`, `result_ref` → `run.subject_ref`, answers → memory seam, ready steps spawn (a failing spawn rolls the approval back) |
| regenerate | a step in `review` or `failed`: its pending proposal is superseded, a new task gets the same params plus `previous` (that value) and the operator's `instruction` |
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

All three take the step params (`kinds/step.py`: `channel, run, step, brief, inputs, prompt, rules,
instruction, previous`); templates preset `prompt` / `rules` and the kind's own keys.

| Kind | Own params | Call | Value |
|---|---|---|---|
| `generate` | `format: markdown \| text \| json`, `json_schema` (an object; json only) | one completion | `{"text"}`, or `{"data"}` shaped by the schema |
| `questions` | — | one completion, schema `{questions: [{id, question, why, type: text\|choice, choices, optional}]}`; sees profile facts and remembered answers | `{"questions": [...], "answers": {}}` (ids made unique, `choice` without choices → `text`) |
| `compose` | `body_step` (`draft`), `meta_step` (`meta`), `language` (blank = `brief.language`) | none (cost 0) | `{language, title, slug, body_markdown, meta, faq}`; title = meta title, else the first `# heading`, else `brief.title`; slug slugified; `meta` = the meta data without title/slug/faq |

**Answers.** The operator fills `answers` (`{question id: text}`) with a manual edit (`PATCH proposals/{id}/`)
and approves. A non-optional question without an answer → 400 `ANSWERS_MISSING` (`details[].field` = the
question ids), a `choice` answer outside its choices → 400.

**Prompt.** System = worker system prompt + profile instructions. User = rules, the template prompt, a
data note, fenced data blocks (`brief`, `inputs`, `company_profile`, augmentation blocks), the kind's task,
then for a regeneration the fenced `previous_version` and the operator instruction.

## Seams for profile, memory and augmentations (`kinds/seams.py`)

Looked up when used; absent → the section is simply missing. Plans for the profile, memory and
augmentations fill them:

| Seam | Callable |
|---|---|
| profile | `services.profile_service.context_for(*, channel, task_type, kind) -> (instructions, data)` |
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
| `POST` | `runs/{uid}/steps/{key}/regenerate/` | `{instruction?}` → 200 run; 400 step not in review/failed or run closed; 404 unknown step. Throttled |
| `POST` | `runs/{uid}/cancel/` | → 200 run; 400 when already done/cancelled |

A step's output is read and edited through the proposal endpoints (`proposal_id`).
