---
title: ComfyUI Workers
description: Kind comfy — a worker is a ComfyUI workflow is a task type; picture checks as verdicts, picture processing as new pictures.
---

A `comfy` worker runs one ComfyUI workflow on a product's main picture and turns the answer into proposals. A worker
is a workflow is a task type: started from the CMS like any other enricher task (a list of products, a filter, a
spawn rule), watched in the Runs list, its proposals reviewed in Review, its results on the Picture checks screen. On
top of that the `enrichment.comfy_spawn` beat walks every unchecked main picture of the channel on its own, inside the
worker's time window.

Two outputs:

- **`metrics`** — a check. A metric the host registers turns the workflow's output into numbers; thresholds turn the
  score into a verdict (a flag set at once, a flag for review, an escalation, or nothing wrong). Example: BiRefNet mask
  → "the light product disappears on a grey tile".
- **`picture`** — processing. The workflow's output is a new picture, proposed as a derived picture of the main one,
  always for a person to accept. Coded and tested against a fake ComfyUI, but it has **never run on a real workflow**
  yet (see [Known limits](#known-limits)).

The bus owns the mechanics. The host owns the domain: the workflow file, the metric and prefilter functions, the
thresholds and the features. ComfyUI is called directly (`ENRICHMENT_COMFYUI_URL`, the plain ComfyUI HTTP API, no
auth), not through the AI toolbox: it runs in the client's own network, on a GPU the client may share.

## A worker

A `Worker` row (`kind=comfy`, `mode=internal`, `target_module` = the adapter, `key` = the task type) with these
`options` (`ComfyOptions`, validated on save):

| Option | Meaning | On change |
|---|---|---|
| `channel` | channel whose main pictures are read | version bump |
| `workflow` | `workflow.json` in ComfyUI API format; a relative path is read from the host's `BASE_DIR` | runs again |
| `input_node` | id of the `LoadImage` node the picture goes into | runs again |
| `output` | `metrics` or `picture` | runs again |
| `metric`, `score` | registered metric name; the key of its result the thresholds compare (default `score`) | runs again |
| `prefilter` | registered prefilter, optional: a verdict without the GPU | runs again |
| `thresholds` | `[{min, action, reason?}]`, `action` = `set` / `review` / `escalate`; the highest `min` the score reaches wins, none = `pass` | re-judged |
| `feature` | BOOL feature of `set` / `review` / `pass` | re-judged |
| `escalate_feature`, `escalate_ok` | select feature of `escalate` (the option = the reason); the option written back when a picture is fine again | re-judged |
| `dry_run` | results only, no proposals (a first run on production); `metrics` workflows only | re-judged |
| `window` | local time `HH:MM-HH:MM` a task may start in (`22:00-06:00` spans midnight; start = end is refused); blank = any time | nothing |
| `batch` | pictures per task the spawn beat starts (1-500, default 100) | nothing |
| `auto_spawn` | the beat walks every unchecked main picture (default on); off = tasks by hand or by spawn rules only | nothing |

"Runs again" options are in the run hash (below): every picture goes through the workflow again. "Re-judged" options
bump the worker version: every picture is due again, but a stored score is judged against the new thresholds without
the GPU. `window`, `batch` and `auto_spawn` only schedule work: no version bump, no re-run.

The host registers its functions from its own `AppConfig.ready()`, before any worker is saved:

```python
from django_enrichment.kinds.comfy import Prefiltered, register_comfy_metric, register_comfy_prefilter

register_comfy_metric("contour_lost", contour_lost)          # (input bytes, output bytes) -> {"score": 0.42, ...}
register_comfy_prefilter("packshot_format", packshot_format)  # input bytes -> Prefiltered("pass" | "escalate", reason) | None
```

Adding a workflow is: export it from ComfyUI in API format, ship the file with the host, register the metric (a
`metrics` workflow), add a worker in Django admin. A `picture` workflow needs no host code at all.

## Running it

A `comfy` task is an ordinary task, with two rules of its own because ComfyUI is one GPU:

- **Always the bulk lane.** Whatever the caller asks for (`lane: now` too), every task of a `comfy` worker runs on
  `queue` (`ENRICHMENT_BULK_QUEUE`, concurrency 1): one picture on the GPU at a time.
- **Only inside the window.** A task created outside `window` stays `open` with no units; the reaper
  (`enrichment.reap`, every minute) offers it to the planner again once the window is open, and it is planned then.
  A task that started inside the window runs to its end.

Ways to start one:

| From | Scope |
|---|---|
| CMS, PIM product grid → AI task → the worker | the selected products (`list`), or every product matching the grid's filters (`filter`, e.g. `attribute_filters: {"packshot_check": "replace_size"}`) |
| CMS, Studio → New → Tasks | pasted SKUs, a category |
| CMS, spawn rule with the worker as task type | a PIM gap check (`gap`) |
| `enrichment.comfy_spawn` beat | the next `batch` unchecked main pictures (`list`), when `auto_spawn` |
| `POST tasks/` | any scope; `params` is `{}` (the worker's options say everything) |

A stored result stops a second run of the same file whatever started the task: the picture is judged again from the
stored metrics, without the GPU.

### Progress: `task.counts`

Per picture the kind bumps, also in a dry run:

| Key | Meaning |
|---|---|
| `checked` | the picture got a verdict |
| `set` / `review` / `pass` / `escalate` / `propose_picture` | that verdict |
| `cached` | from a stored result, no GPU |
| `prefiltered` | from the prefilter, no GPU |

Besides the bus's own `failed` (ComfyUI down, timeout, unreadable picture: `task.errors` has the code), `proposed` and
`kept_edited`. A task of 100 pictures that all came back shows `checked: 100` split by verdict. A picture is counted
once per task: a unit run again after a lost lease finds the result its task already stored and does not count it
again. The counters are what was checked, so a task cancelled mid-unit keeps them although that unit's proposals are
discarded.

Unlike the text kinds, a `comfy` worker runs a product even when an operator edited one of its proposals there
(`Kind.skip_edited_subjects = False`): every picture must get a stored result, or the spawn would offer it forever.
The edited proposal itself is kept at intake (`kept_edited`).

## Verdicts and proposals

| Verdict | Proposal |
|---|---|
| `set` | `feature` = true, applied at once (`auto_apply`) |
| `review` | `feature` = true, pending |
| `pass` | `feature` = false, pending, only when it is true now (whoever set it) |
| `escalate` | `escalate_feature` = the reason, applied at once |
| any other, after an escalation | `escalate_feature` = `escalate_ok`, applied at once, only over an option this worker applied itself (never a person's or another worker's) |
| `propose_picture` (`output: picture`) | the output as a derived picture of the main one, always pending |

A value that is already there gets no proposal. Every proposal carries `source = worker:<key>@v<version>`, the drift
check and undo like any other. `auto_apply` is a field of `ProposalDraft`: the runner applies such a proposal right
after intake; a write the adapter refuses (a feature outside the product's feature set) leaves it pending and logs. A
media draft can never be applied automatically (`ValueError` at construction): a new picture always waits for a
person.

**A derived picture on accept (PIM adapter).** The proposal's locator is `{channel, source_picture_ref, operation:
<worker key>, variant: 1}`, its value `{op: "derived", operation, preset: "", variant: 1, of: 1}`; the staged file is
a PNG named after the worker. The adapter offers two accept modes (`apply_modes`): **add** (default, also for bulk
accept) links it as a new gallery picture after the others; **replace** swaps the file of the source picture's link
(the main one), keeping its role, position and alt texts; the old `Picture` stays as the undo anchor until retention.
Undo removes the added link, or puts the old file back. A source picture removed from the gallery in the meantime
refuses replace ("add the new one instead").

## Results: `ComfyResult`

One row per (product, file sha1, worker, run hash) with the metrics, the verdict and reason, its source (`prefilter` /
`workflow`), the worker version and the task that judged it last. The run hash is the sha256 of `workflow.json` and of
the options that shape a result: `input_node`, `output`, `metric`, `score`, `prefilter`. A row is written only after
an answer: ComfyUI down, a timeout or a failed prompt is a subject failure (`task.errors`), no row, no proposal, and
the next spawn takes the picture again. Nothing is judged without an answer.

- New main picture = new sha1 = checked again.
- A proposed picture whose proposal is gone (an intake that failed) runs again; the output itself is not kept.
- The same file on two products is a row each and runs twice: each product needs its own proposals.
- A changed metric or prefilter function under the same name (host code) is invisible to the bus: delete the rows
  (Django admin) to re-run, or register it under a new name.
- Deleting a row re-runs that picture on the next spawn.

### In the CMS

Review → **Picture checks** lists the results (enrichment 2.5.0+): a thumbnail of the very file that was checked, the
verdict, the score and the other metrics, the product, the task. Filter by worker, verdict (chips with totals), source,
reason, SKU, task (the Runs list links a `comfy` task to its results) and a score range; sort by score when one worker
is chosen; export the list as CSV. A row links to the product's proposals of that worker in Review, applied ones too,
to undo a wrong `set` or escalation. That is the threshold review of a dry run: narrow to a score range, look where
`review` should start, set the thresholds, re-judge without the GPU.

API (`IsAdminUser`, area `enrichment.proposals`):

| Endpoint | Meaning |
|---|---|
| `GET comfy-results/` | paginated; `worker`, `verdict`, `source`, `reason`, `subject_ref` (exact), `search` (part of the SKU), `task`, `score_min` / `score_max`, `latest` (default true), `ordering` (`-checked_at` default, `checked_at`, `score`, `-score`; no score last). Scores compare within one worker only |
| `GET comfy-results/verdicts/` | `{total, verdicts: {verdict: n}, reasons: {reason: n}}` under the same filters; each total ignores its own filter |
| `GET comfy-results/export/` | the whole filtered list as CSV (at most 50 000 rows): worker, SKU, verdict, reason, score, source, worker version, task, checked at, sha1, then one `metric_<key>` column per metric; a text cell starting like a formula gets a leading `'` |

`latest` (default) lists only the newest check of each product per worker: a product whose main picture was replaced,
or that ran again under an edited workflow, keeps its older results as history (`latest=false`). A picture that comes
back (A, B, A) is offered by the spawn again and re-judged from its stored result, so the newest row is always the
picture the product has. The export also answers `Accept: text/csv`.

Each item has `score` (the stored `ComfyResult.score`: `metrics[<worker's score>]` when it is a number, else null) and `thumbnail_url` from the adapter's optional extra
`pictures_by_sha1(*, sha1s) -> {sha1: url}` (PIM 3.10.0), one call per page; blank without the extra or once the file
is gone.

## Spawn: `enrichment.comfy_spawn`

The host schedules the beat around the clock; each worker's `window` is the time window:

```python
"enrichment.comfy_spawn": {
    "task": "enrichment.comfy_spawn",
    "schedule": crontab(minute="*/15"),
    "options": {"queue": "enrichment"},
},
```

For each active, unpaused internal `comfy` worker with `auto_spawn`, inside its `window`, with no task open or in
progress, and only when ComfyUI answers `/system_stats`, it pages the adapter's `main_pictures(channel, page)`
(`[{subject_ref, picture_ref, sha1, width, height}]`), keeps the products whose (product, sha1) has no result under the
current run hash and worker version, and spawns one `list` task of up to `batch` of them. Products of the worker's
previous task that are still unchecked (they failed there) go after every other, so pictures that fail every time do
not hold the rest back and are still retried. A picture listed without a sha1 is skipped.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `ENRICHMENT_COMFYUI_URL` | `""` | ComfyUI base URL; blank = the spawn does nothing, a run fails with `COMFYUI_NOT_CONFIGURED` |
| `ENRICHMENT_COMFYUI_TIMEOUT_S` | `180` | how long one prompt may take, queue time on a shared GPU included |

`ENRICHMENT_COMFY_SPAWN_LIMIT` (2.4.0) is gone: the batch is the worker's `batch` option.

## Failure codes

| Code | Meaning |
|---|---|
| `COMFYUI_UNAVAILABLE` | ComfyUI cannot be reached |
| `COMFYUI_HTTP_<status>` | ComfyUI refused a request (a workflow with a node it does not know: 400 on `/prompt`) |
| `COMFYUI_FAILED` | the prompt ran and failed |
| `COMFYUI_NO_OUTPUT` | the workflow saved no image |
| `COMFYUI_NOT_CONFIGURED` | `ENRICHMENT_COMFYUI_URL` is blank |
| `TIMEOUT` | no answer within `ENRICHMENT_COMFYUI_TIMEOUT_S`; the prompt is taken off ComfyUI's queue first (a running one finishes), then retried once in place |
| `COMFYUI_BAD_RESPONSE` | a 2xx answer that is not JSON or not the shape ComfyUI sends (a proxy page) |
| `COMFY_WORKFLOW_MISSING` / `COMFY_WORKFLOW_INVALID` | the workflow file cannot be read, is not JSON, or has no `input_node` |
| `COMFY_NO_SCORE` | the metric's result has no number under `score` |

## Known limits

- **`output: picture` has never run on a real workflow.** The path (workflow output → staged PNG → pending derived
  picture → add / replace on accept) is tested end to end against a fake ComfyUI and the PIM adapter's derived-picture
  apply is the one the `image` kind uses, but no real cut-out or upscale workflow went through it yet. The first real
  one should start on a test host with a handful of products.
- **No workflow parameters.** The workflow file is fixed per worker; a prompt text, a size or a seed cannot come from
  the task. A second variant is a second worker with its own file.
- **No "replace the main picture" as such.** A processed picture is always a derived picture: accept with *replace*
  swaps the main picture's file, *add* adds it to the gallery. There is no `replace_main` operation.
- **No output checks.** Size, format and background of the output are what the workflow makes (always saved as PNG);
  the bus does not check them against a standard. Build them into the workflow, or check them with a second, `metrics`
  worker.
- **Main picture only.** Gallery pictures other than the main one are never read.
- **Escalation is a product feature**, not a gap finding: the client filters on the select feature.
