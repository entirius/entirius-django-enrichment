---
title: ComfyUI Picture Checks
description: Kind comfy — every main picture through a ComfyUI workflow once per file, verdicts as flags, escalations or new pictures.
---

Kind `comfy` runs a ComfyUI workflow on the main picture of every product in a channel, once per file, and turns the
answer into proposals. Two uses so far: a check (BiRefNet mask → "the light product disappears on a grey tile" →
a BOOL flag) and a processing step (upscale → a new picture for a person to accept). The bus owns the mechanics; the
host owns the domain: the workflow file, the metric and the prefilter, the thresholds and the features.

ComfyUI is called directly (`ENRICHMENT_COMFYUI_URL`, the plain ComfyUI HTTP API, no auth), not through the AI
toolbox: it runs in the client's own network, on a GPU the client may share.

## One worker per workflow

A `Worker` row (`kind=comfy`, `mode=internal`, `target_module` = the adapter) with these `options`
(`ComfyOptions`, validated on save; a change bumps the worker version):

| Option | Meaning |
|---|---|
| `channel` | channel whose main pictures are checked |
| `workflow` | `workflow.json` in ComfyUI API format; a relative path is read from the host's `BASE_DIR` |
| `input_node` | id of the `LoadImage` node the picture goes into |
| `output` | `metrics` (judge the output with a metric) or `picture` (the output is a new picture) |
| `metric`, `score` | registered metric name; the key of its result the thresholds compare (default `score`) |
| `prefilter` | registered prefilter, optional: a verdict without the GPU |
| `thresholds` | `[{min, action, reason?}]`, `action` = `set` / `review` / `escalate`; the highest `min` the score reaches wins, none = `pass` |
| `feature` | BOOL feature of `set` / `review` / `pass` |
| `escalate_feature`, `escalate_ok` | select feature of `escalate` (the option = the reason); the option written back when a picture is fine again |
| `dry_run` | results only, no proposals (a first run on production); `metrics` workflows only |

The host registers its functions from its own `AppConfig.ready()`, before any worker is saved:

```python
from django_enrichment.kinds.comfy import Prefiltered, register_comfy_metric, register_comfy_prefilter

register_comfy_metric("contour_lost", contour_lost)          # (input bytes, output bytes) -> {"score": 0.42, ...}
register_comfy_prefilter("packshot_format", packshot_format)  # input bytes -> Prefiltered("pass" | "escalate", reason) | None
```

## Verdicts and proposals

| Verdict | Proposal |
|---|---|
| `set` | `feature` = true, applied at once (`auto_apply`) |
| `review` | `feature` = true, pending |
| `pass` | `feature` = false, pending, only when it is true now (whoever set it) |
| `escalate` | `escalate_feature` = the reason, applied at once |
| any other, after an escalation | `escalate_feature` = `escalate_ok`, applied at once, only over an option this worker applied itself (never a person's or another worker's) |
| `propose_picture` (`output: picture`) | the output as a derived picture of the main one (`op=derived`, `source_picture_ref`), always pending: add or replace on accept |

A value that is already there gets no proposal. Every proposal carries `source = worker:<key>@v<version>`, the drift
check and undo like any other. `auto_apply` is a field of `ProposalDraft`: the runner applies such a proposal right
after intake; a write the adapter refuses (a feature outside the product's feature set) leaves it pending and logs.
A media draft can never be applied automatically (`ValueError` at construction).

## Results: `ComfyResult`

One row per (product, file sha1, worker, run hash) with the metrics, the verdict, its source (`prefilter` /
`workflow`) and the worker version. The run hash is the sha256 of `workflow.json` and of the options that shape a
result: `input_node`, `output`, `metric`, `score`, `prefilter`. A row is written only after an answer: ComfyUI down, a
timeout or a failed prompt is a subject failure (`task.errors`), no row, no proposal, and the next spawn takes the
picture again. Nothing is judged without an answer.

- New main picture = new sha1 = checked again. An edited workflow file, or a change of a run-shaping option, checks
  every picture again.
- New thresholds or features (a worker version bump): every picture is due again, but a stored workflow score is
  judged against the new thresholds without running the workflow. A prefilter verdict stands.
- A proposed picture whose proposal is gone (an intake that failed) runs again; the output itself is not kept.
- The same file on two products is a row each and runs twice: each product needs its own proposals, and shared
  pictures are rare.
- A changed metric or prefilter function under the same name (host code) is invisible to the bus: delete the rows
  (Django admin) to re-run, or register it under a new name.
- Deleting a row re-runs that picture on the next spawn.

## Spawn: `enrichment.comfy_spawn`

The host schedules the beat; its schedule is the time window (a shared GPU at night only):

```python
"enrichment.comfy_spawn": {
    "task": "enrichment.comfy_spawn",
    "schedule": crontab(minute="*/15", hour="22-23,0-5"),
    "options": {"queue": "enrichment"},
},
```

For each active, unpaused internal `comfy` worker with no task open or in progress, and only when ComfyUI answers
`/system_stats`, it pages the adapter's `main_pictures(channel, page)` (`[{subject_ref, picture_ref, sha1, width,
height}]`), keeps the products whose (product, sha1) has no result under the current workflow and worker version, and
spawns one `list` task of up to `ENRICHMENT_COMFY_SPAWN_LIMIT` (100) of them on the bulk lane (`ENRICHMENT_BULK_QUEUE`,
one at a time = one picture on the GPU at a time). Products of the worker's previous task that are still unchecked
(they failed there) go after every other, so pictures that fail every time do not hold the rest back and are still
retried. A picture listed without a sha1 is skipped.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `ENRICHMENT_COMFYUI_URL` | `""` | ComfyUI base URL; blank = the spawn does nothing, a run fails with `COMFYUI_NOT_CONFIGURED` |
| `ENRICHMENT_COMFYUI_TIMEOUT_S` | `180` | how long one prompt may take, queue time on a shared GPU included |
| `ENRICHMENT_COMFY_SPAWN_LIMIT` | `100` | pictures per spawned task |

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
