# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `comfy` — check or process a subject's main picture with a ComfyUI workflow, once per file.

One `Worker` per workflow; everything about it sits in `Worker.options` (`ComfyOptions`), so a changed threshold bumps
the worker version. The host owns the domain: the workflow file, and the metric and prefilter functions it registers
by name from its own `AppConfig.ready()` (`register_comfy_metric`, `register_comfy_prefilter`) — the bus knows no
metric.

Per subject: the main picture (adapter `list_pictures` + `picture`) → its sha1 → the stored `ComfyResult` of (product,
sha1, worker, run hash), else the prefilter (no GPU), else the workflow on ComfyUI (`comfyui_client`) and the metric on
its output. The run hash covers the workflow file and the options that shape a result (`RUN_FIELDS`), so changing any of
them runs again. The verdict is the first threshold the score reaches (highest first), else `pass`; a stored result is
re-judged against the current thresholds without running the workflow again.

Verdict → drafts (none with `dry_run`; none when the value is already there):
- `set`: `feature` = true, applied at once (`auto_apply`);
- `review`: `feature` = true, pending;
- `pass`: `feature` = false, pending — only when it is true now (a flag the worker no longer supports goes back to a
  person, whoever set it);
- `escalate`: `escalate_feature` = the reason (an option of that select), applied at once; any other verdict puts
  `escalate_ok` back the same way, but only over an option this worker applied itself (a replaced picture clears its
  own escalation, never a person's or another worker's);
- `propose_picture` (`output: picture`): the workflow's output as a derived picture of the main one, always pending;
  the workflow runs again for a picture whose proposal never landed.

ComfyUI down or failing raises (a subject failure with a stable code): no result, no draft, the next spawn tries the
file again. Nothing is ever judged without an answer.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time
from typing import Any, Literal

from django.core.files.base import ContentFile
from django.utils import timezone
from pydantic import BaseModel, Field, model_validator

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds import extras
from django_enrichment.kinds.base import Kind, ProposalDraft, SubjectError
from django_enrichment.kinds.image import DERIVED_OP, main_picture_ref, read_picture
from django_enrichment.services import comfy_result_service, comfyui_client, task_service

KIND = "comfy"
SET, REVIEW, ESCALATE, PASS, PROPOSE_PICTURE = "set", "review", "escalate", "pass", "propose_picture"
# The options that shape a stored result: a change runs the workflow again (thresholds and features only re-judge).
RUN_FIELDS = frozenset({"input_node", "output", "metric", "score", "prefilter"})
# The options that only schedule work: a change neither runs again nor bumps the worker version.
SCHEDULE_FIELDS = frozenset({"window", "batch", "auto_spawn"})
_WINDOW = r"^$|^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"
DEFAULT_BATCH, MAX_BATCH = 100, 500
SOURCE_PREFILTER, SOURCE_WORKFLOW = "prefilter", "workflow"
# Task counters (`task.counts`) besides one per verdict: every subject with a verdict, a stored result reused, a
# prefilter verdict. A subject that failed is in the bus's own `failed`.
CHECKED, CACHED, PREFILTERED = "checked", "cached", "prefiltered"
FLAG_KIND, PICTURE_KIND = "attribute_value", "picture"
NO_SCORE = "COMFY_NO_SCORE"


@dataclass(frozen=True)
class Prefiltered:
    """A prefilter's verdict without the GPU: `pass`, or `escalate` with the reason (an option of the escalation
    select). `metrics` are stored with it."""

    action: Literal["pass", "escalate"]
    reason: str = ""
    metrics: dict = field(default_factory=dict)


Metric = Callable[[bytes, bytes], dict[str, float]]  # (input picture, workflow output) → metrics
Prefilter = Callable[[bytes], Prefiltered | None]  # input picture → a verdict, or None = run the workflow

_METRICS: dict[str, Metric] = {}
_PREFILTERS: dict[str, Prefilter] = {}


def register_comfy_metric(name: str, metric: Metric) -> None:
    _METRICS[name] = metric


def register_comfy_prefilter(name: str, prefilter: Prefilter) -> None:
    _PREFILTERS[name] = prefilter


class Threshold(BaseModel):
    min: float = Field(description="The score reaching this takes the action", examples=[0.3])
    action: Literal["set", "review", "escalate"]
    reason: str = Field("", max_length=64, description="Option written by `escalate`", examples=["replace_size"])


class ComfyOptions(BaseModel):
    channel: str = Field(min_length=1, description="Channel idx whose main pictures are checked")
    workflow: str = Field(min_length=1, description="workflow.json (ComfyUI API format); relative = to BASE_DIR")
    input_node: str = Field(min_length=1, description="Id of the LoadImage node the picture goes into")
    output: Literal["metrics", "picture"] = "metrics"
    metric: str = Field("", description="Registered metric (output `metrics`)")
    score: str = Field("score", description="The metric value the thresholds compare")
    prefilter: str = Field("", description="Registered prefilter; blank = none")
    thresholds: list[Threshold] = Field(default_factory=list)
    feature: str = Field("", description="BOOL feature of `set` / `review` / `pass`")
    escalate_feature: str = Field("", description="Select feature of `escalate`")
    escalate_ok: str = Field("", max_length=64, description="Option of `escalate_feature` for a picture now fine")
    dry_run: bool = Field(False, description="Store results only, no proposals")
    window: str = Field(
        "",
        pattern=_WINDOW,
        description="Local time `HH:MM-HH:MM` a task of this worker may start in (may span midnight); blank = any time",
        examples=["22:00-06:00"],
    )
    batch: int = Field(DEFAULT_BATCH, ge=1, le=MAX_BATCH, description="Pictures per task the spawn beat starts")
    auto_spawn: bool = Field(True, description="The spawn beat checks every unchecked main picture; off = by hand only")

    @model_validator(mode="after")
    def _consistent(self) -> "ComfyOptions":
        _check_registered(self)
        actions = {threshold.action for threshold in self.thresholds}
        if self.output == "picture" and (actions or self.dry_run):
            raise ValueError("a picture workflow takes no thresholds and no dry run (its output is always proposed)")
        if actions & {SET, REVIEW} and not self.feature:
            raise ValueError("`set` / `review` thresholds need `feature`")
        if ESCALATE in actions and not self.escalate_feature:
            raise ValueError("an `escalate` threshold needs `escalate_feature`")
        if any(t.action == ESCALATE and not t.reason for t in self.thresholds):
            raise ValueError("an `escalate` threshold needs a `reason`")
        if self.window and len(set(self.window.split("-"))) == 1:
            raise ValueError("a window that starts when it ends never opens; leave it blank for any time")
        return self


def _check_registered(options: ComfyOptions) -> None:
    if options.output == "metrics" and options.metric not in _METRICS:
        raise ValueError(f"unknown metric {options.metric!r} (registered: {sorted(_METRICS)})")
    if options.prefilter and options.prefilter not in _PREFILTERS:
        raise ValueError(f"unknown prefilter {options.prefilter!r} (registered: {sorted(_PREFILTERS)})")


class ComfyParams(BaseModel):
    """No run parameters: the spawn names the subjects, the worker's options say the rest."""


@dataclass(frozen=True)
class Outcome:
    verdict: str
    reason: str = ""
    picture: bytes | None = None  # the workflow's output, only fresh from ComfyUI with output `picture`
    origin: str = SOURCE_WORKFLOW  # `cached` (a stored result) | `prefilter` | `workflow`


@dataclass(frozen=True)
class Run:
    """One subject's run: the worker, the task it counts in (the result remembers it), the validated options."""

    worker: Any
    task: Any
    options: ComfyOptions


@dataclass(frozen=True)
class Subject:
    ref: str
    picture_ref: str
    image: bytes
    mime: str


class ComfyKind(Kind):
    Options = ComfyOptions
    Params = ComfyParams
    lane = "queue"  # the bulk lane, one task at a time: ComfyUI is one GPU
    unversioned_options = SCHEDULE_FIELDS
    # Every picture must get a stored result, or the spawn offers it again forever; an edited flag keeps its value at
    # intake anyway (`kept_edited`).
    skip_edited_subjects = False

    def may_start(self, worker: Any) -> bool:
        # The window alone, not the whole options: the reaper asks for every worker every minute.
        return in_window((worker.options or {}).get("window", ""), _local_now())

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        options = ComfyOptions.model_validate(worker.options)
        adapter = get_adapter(worker.target_module)
        picture_ref = main_picture_ref(adapter, subject_ref, options.channel)
        subject = Subject(subject_ref, picture_ref, *read_picture(adapter, subject_ref, options.channel, picture_ref))
        workflow, run_hash = load_run(options)
        sha1 = hashlib.sha1(subject.image, usedforsecurity=False).hexdigest()
        key = comfy_result_service.ResultKey(subject_ref, sha1, worker.key, run_hash)
        stored = comfy_result_service.find(key)
        outcome = _outcome(Run(worker, task, options), key, subject, workflow, stored)
        if stored is None or stored.task_id != task.pk:  # a unit run again (lost lease) counts its pictures once
            _count(task, outcome)
        return [] if options.dry_run else _drafts(adapter, worker, options, subject, outcome)


def _local_now() -> time:
    return timezone.localtime().time()


def in_window(window: str, now: time) -> bool:
    """`now` inside `HH:MM-HH:MM` (start included, end excluded; a start after the end spans midnight)."""
    if not window:
        return True
    start, end = (time.fromisoformat(part) for part in window.split("-"))
    return start <= now < end if start <= end else now >= start or now < end


def load_run(options: ComfyOptions) -> tuple[dict, str]:
    """The workflow and the run hash results are keyed by: the workflow file and the options in `RUN_FIELDS`."""
    workflow, run_hash = comfy_result_service.load_workflow(options.workflow, options.model_dump(include=RUN_FIELDS))
    if options.input_node not in workflow:
        raise SubjectError(comfy_result_service.WORKFLOW_INVALID, f"no node {options.input_node!r}")
    return workflow, run_hash


def _outcome(run: Run, key: Any, subject: Subject, workflow: dict, stored: Any) -> Outcome:
    """The stored result re-judged, else a new one (prefilter, else the workflow); stored before any draft. A stored
    picture whose proposal never landed (an intake that failed) runs again: the output itself is not kept."""
    prefilter = run.options.prefilter
    if stored is not None and (stored.verdict != PROPOSE_PICTURE or _picture_proposed(run, subject)):
        return _rejudge(run, key, stored)
    if prefilter and (decision := _PREFILTERS[prefilter](subject.image)):
        outcome = Outcome(decision.action, decision.reason, origin=SOURCE_PREFILTER)
        _store(run, key, outcome, source=SOURCE_PREFILTER, metrics=decision.metrics)
        return outcome
    return _run_workflow(run, key, subject, workflow)


def _rejudge(run: Run, key: Any, stored: Any) -> Outcome:
    """A stored workflow score meets the current thresholds; a prefilter verdict and a proposed picture stand."""
    if stored.source == SOURCE_WORKFLOW and run.options.output == "metrics":
        outcome = _judge(run.options, stored.metrics)
    else:
        outcome = Outcome(stored.verdict, stored.reason)
    _store(run, key, outcome, source=stored.source, metrics=stored.metrics)
    return Outcome(outcome.verdict, outcome.reason, origin=CACHED)


def _run_workflow(run: Run, key: Any, subject: Subject, workflow: dict) -> Outcome:
    options = run.options
    filename = f"enrichment-{key.input_sha1}.{subject.mime.rsplit('/', 1)[-1]}"
    output = comfyui_client.run(workflow, input_node=options.input_node, image=subject.image, filename=filename)
    if options.output == "picture":
        _store(run, key, Outcome(PROPOSE_PICTURE), source=SOURCE_WORKFLOW, metrics={})
        return Outcome(PROPOSE_PICTURE, picture=output)
    metrics = _METRICS[options.metric](subject.image, output)
    outcome = _judge(options, metrics)
    _store(run, key, outcome, source=SOURCE_WORKFLOW, metrics=metrics)
    return outcome


def _judge(options: ComfyOptions, metrics: dict) -> Outcome:
    if not isinstance(score := metrics.get(options.score), int | float):
        raise SubjectError(NO_SCORE, f"the metric gave no number {options.score!r}")
    for threshold in sorted(options.thresholds, key=lambda t: t.min, reverse=True):
        if score >= threshold.min:
            return Outcome(threshold.action, threshold.reason)
    return Outcome(PASS)


def _count(task: Any, outcome: Outcome) -> None:
    """`checked`, the verdict, and where it came from when the GPU was not needed — also in a dry run."""
    bumps = {CHECKED: 1, outcome.verdict: 1}
    if outcome.origin == CACHED:
        bumps[CACHED] = 1
    elif outcome.origin == SOURCE_PREFILTER:
        bumps[PREFILTERED] = 1
    task_service.increment_counts(task, bumps)


def _store(run: Run, key: Any, outcome: Outcome, *, source: str, metrics: dict) -> None:
    comfy_result_service.save(
        key,
        source=source,
        metrics=metrics,
        verdict=outcome.verdict,
        reason=outcome.reason,
        worker_version=run.worker.version,
        task=run.task,
    )


def _drafts(adapter: Any, worker: Any, options: ComfyOptions, subject: Subject, outcome: Outcome) -> list:
    if outcome.verdict == PROPOSE_PICTURE:
        return [_picture_draft(worker, options, subject, outcome.picture)] if outcome.picture else []
    drafts = (
        _flag_draft(adapter, options, subject, outcome),
        _escalation_draft(adapter, worker, options, subject, outcome),
    )
    return [draft for draft in drafts if draft is not None]


def _flag_draft(adapter: Any, options: ComfyOptions, subject: Subject, outcome: Outcome) -> ProposalDraft | None:
    if not options.feature or outcome.verdict == ESCALATE:
        return None
    locator, current = _current(adapter, options, subject, options.feature)
    if outcome.verdict in (SET, REVIEW) and current != {"value": True}:
        return ProposalDraft(FLAG_KIND, locator, {"value": True}, auto_apply=outcome.verdict == SET)
    if outcome.verdict == PASS and current == {"value": True}:
        return ProposalDraft(FLAG_KIND, locator, {"value": False})
    return None


def _escalation_draft(
    adapter: Any, worker: Any, options: ComfyOptions, subject: Subject, outcome: Outcome
) -> ProposalDraft | None:
    """The escalation option, or `escalate_ok` back over an option this worker applied itself."""
    wanted = outcome.reason if outcome.verdict == ESCALATE else options.escalate_ok
    if not (options.escalate_feature and wanted):
        return None
    locator, current = _current(adapter, options, subject, options.escalate_feature)
    if current.get("option") == wanted:
        return None
    if outcome.verdict != ESCALATE and not _applied_by(worker, subject, locator, current):
        return None  # empty, or set by a person or another worker: not ours to put back
    return ProposalDraft(FLAG_KIND, locator, {"option": wanted}, auto_apply=True)


def _applied_by(worker: Any, subject: Subject, locator: dict, current: dict) -> bool:
    last = comfy_result_service.last_applied(worker.target_module, subject.ref, FLAG_KIND, locator)
    return last is not None and last.source.startswith(f"worker:{worker.key}@") and last.proposed_value == current


def _current(adapter: Any, options: ComfyOptions, subject: Subject, feature: str) -> tuple[dict, dict]:
    locator = extras.locator(adapter, field_key=feature, channel=options.channel, language=None)
    current = adapter.read_current(subject_ref=subject.ref, target_kind=FLAG_KIND, target_locator=locator)
    return locator, current


def _picture_locator(worker: Any, options: ComfyOptions, subject: Subject) -> dict:
    return {
        "channel": options.channel,
        "source_picture_ref": subject.picture_ref,
        "operation": worker.key,
        "variant": 1,
    }


def _picture_proposed(run: Run, subject: Subject) -> bool:
    locator = _picture_locator(run.worker, run.options, subject)
    return comfy_result_service.proposal_exists(run.worker.target_module, subject.ref, PICTURE_KIND, locator)


def _picture_draft(worker: Any, options: ComfyOptions, subject: Subject, png: bytes) -> ProposalDraft:
    value = {"op": DERIVED_OP, "operation": worker.key, "preset": "", "variant": 1, "of": 1}
    locator = _picture_locator(worker, options, subject)
    return ProposalDraft(PICTURE_KIND, locator, value, media_file=ContentFile(png, name=f"{worker.key}.png"))
