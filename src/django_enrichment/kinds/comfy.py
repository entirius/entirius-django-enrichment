# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `comfy` — check or process a subject's main picture with a ComfyUI workflow, once per file.

One `Worker` per workflow; everything about it sits in `Worker.options` (`ComfyOptions`), so a changed threshold bumps
the worker version. The host owns the domain: the workflow file, and the metric and prefilter functions it registers
by name from its own `AppConfig.ready()` (`register_comfy_metric`, `register_comfy_prefilter`) — the bus knows no
metric.

Per subject: the main picture (adapter `list_pictures` + `picture`) → its sha1 → the stored `ComfyResult` of (sha1,
worker, workflow file hash), else the prefilter (no GPU), else the workflow on ComfyUI (`comfyui_client`) and the metric
on its output. The verdict is the first threshold the score reaches (highest first), else `pass`; a stored result is
re-judged against the current thresholds without running the workflow again.

Verdict → drafts (none with `dry_run`; none when the value is already there):
- `set`: `feature` = true, applied at once (`auto_apply`);
- `review`: `feature` = true, pending;
- `pass`: `feature` = false, pending — only when it is true now (a flag the worker no longer supports goes back to a
  person, whoever set it);
- `escalate`: `escalate_feature` = the reason (an option of that select), applied at once; any other verdict puts
  `escalate_ok` back the same way when the feature holds another option (a replaced picture clears its escalation);
- `propose_picture` (`output: picture`): the workflow's output as a derived picture of the main one, always pending.

ComfyUI down or failing raises (a subject failure with a stable code): no result, no draft, the next spawn tries the
file again. Nothing is ever judged without an answer.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from django.core.files.base import ContentFile
from pydantic import BaseModel, Field, model_validator

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds import extras
from django_enrichment.kinds.base import Kind, ProposalDraft, SubjectError
from django_enrichment.kinds.image import main_picture_ref, read_picture
from django_enrichment.services import comfy_result_service, comfyui_client

SET, REVIEW, ESCALATE, PASS, PROPOSE_PICTURE = "set", "review", "escalate", "pass", "propose_picture"
SOURCE_PREFILTER, SOURCE_WORKFLOW = "prefilter", "workflow"
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

    @model_validator(mode="after")
    def _consistent(self) -> "ComfyOptions":
        _check_registered(self)
        actions = {threshold.action for threshold in self.thresholds}
        if self.output == "picture" and actions:
            raise ValueError("a picture workflow takes no thresholds (its output is always proposed)")
        if actions & {SET, REVIEW} and not self.feature:
            raise ValueError("`set` / `review` thresholds need `feature`")
        if ESCALATE in actions and not self.escalate_feature:
            raise ValueError("an `escalate` threshold needs `escalate_feature`")
        if any(t.action == ESCALATE and not t.reason for t in self.thresholds):
            raise ValueError("an `escalate` threshold needs a `reason`")
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


@dataclass(frozen=True)
class Subject:
    ref: str
    picture_ref: str
    image: bytes
    mime: str


class ComfyKind(Kind):
    Options = ComfyOptions
    Params = ComfyParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:  # noqa: ARG002 — task unused
        options = ComfyOptions.model_validate(worker.options)
        adapter = get_adapter(worker.target_module)
        picture_ref = main_picture_ref(adapter, subject_ref, options.channel)
        subject = Subject(subject_ref, picture_ref, *read_picture(adapter, subject_ref, options.channel, picture_ref))
        workflow, workflow_hash = comfy_result_service.load_workflow(options.workflow)
        if options.input_node not in workflow:
            raise SubjectError(comfy_result_service.WORKFLOW_INVALID, f"no node {options.input_node!r}")
        sha1 = hashlib.sha1(subject.image, usedforsecurity=False).hexdigest()
        key = comfy_result_service.ResultKey(subject_ref, sha1, worker.key, workflow_hash)
        outcome = _outcome(worker, options, key, subject, workflow)
        return [] if options.dry_run else _drafts(adapter, worker, options, subject, outcome)


def _outcome(worker: Any, options: ComfyOptions, key: Any, subject: Subject, workflow: dict) -> Outcome:
    """The stored result re-judged (this product's, else the same file's on another product), else a new one
    (prefilter, else the workflow); stored before any draft."""
    stored = comfy_result_service.find(key)
    if stored is None and (shared := comfy_result_service.find_same_file(key)) and shared.verdict != PROPOSE_PICTURE:
        stored = shared  # another product's output picture was not kept, so only a judged file is shared
    if stored is not None:
        return _rejudge(worker, options, key, stored)
    if options.prefilter and (decision := _PREFILTERS[options.prefilter](subject.image)):
        outcome = Outcome(decision.action, decision.reason)
        _store(worker, key, outcome, source=SOURCE_PREFILTER, metrics=decision.metrics)
        return outcome
    return _run_workflow(worker, options, key, subject, workflow)


def _rejudge(worker: Any, options: ComfyOptions, key: Any, stored: Any) -> Outcome:
    """A stored workflow score meets the current thresholds; a prefilter verdict and a proposed picture stand."""
    if stored.source == SOURCE_WORKFLOW and options.output == "metrics":
        outcome = _judge(options, stored.metrics)
    else:
        outcome = Outcome(stored.verdict, stored.reason)  # a picture is not kept: it is not proposed twice
    _store(worker, key, outcome, source=stored.source, metrics=stored.metrics)
    return outcome


def _run_workflow(worker: Any, options: ComfyOptions, key: Any, subject: Subject, workflow: dict) -> Outcome:
    filename = f"enrichment-{key.input_sha1}.{subject.mime.rsplit('/', 1)[-1]}"
    output = comfyui_client.run(workflow, input_node=options.input_node, image=subject.image, filename=filename)
    if options.output == "picture":
        _store(worker, key, Outcome(PROPOSE_PICTURE), source=SOURCE_WORKFLOW, metrics={})
        return Outcome(PROPOSE_PICTURE, picture=output)
    metrics = _METRICS[options.metric](subject.image, output)
    outcome = _judge(options, metrics)
    _store(worker, key, outcome, source=SOURCE_WORKFLOW, metrics=metrics)
    return outcome


def _judge(options: ComfyOptions, metrics: dict) -> Outcome:
    if not isinstance(score := metrics.get(options.score), int | float):
        raise SubjectError(NO_SCORE, f"the metric gave no number {options.score!r}")
    for threshold in sorted(options.thresholds, key=lambda t: t.min, reverse=True):
        if score >= threshold.min:
            return Outcome(threshold.action, threshold.reason)
    return Outcome(PASS)


def _store(worker: Any, key: Any, outcome: Outcome, *, source: str, metrics: dict) -> None:
    comfy_result_service.save(
        key,
        source=source,
        metrics=metrics,
        verdict=outcome.verdict,
        reason=outcome.reason,
        worker_version=worker.version,
    )


def _drafts(adapter: Any, worker: Any, options: ComfyOptions, subject: Subject, outcome: Outcome) -> list:
    if outcome.verdict == PROPOSE_PICTURE:
        return [_picture_draft(worker, options, subject, outcome.picture)] if outcome.picture else []
    drafts = (_flag_draft(adapter, options, subject, outcome), _escalation_draft(adapter, options, subject, outcome))
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


def _escalation_draft(adapter: Any, options: ComfyOptions, subject: Subject, outcome: Outcome) -> ProposalDraft | None:
    wanted = outcome.reason if outcome.verdict == ESCALATE else options.escalate_ok
    if not (options.escalate_feature and wanted):
        return None
    locator, current = _current(adapter, options, subject, options.escalate_feature)
    if current.get("option") == wanted or (outcome.verdict != ESCALATE and not current.get("option")):
        return None  # already so, or nothing to put back
    return ProposalDraft(FLAG_KIND, locator, {"option": wanted}, auto_apply=True)


def _current(adapter: Any, options: ComfyOptions, subject: Subject, feature: str) -> tuple[dict, dict]:
    locator = extras.locator(adapter, field_key=feature, channel=options.channel, language=None)
    current = adapter.read_current(subject_ref=subject.ref, target_kind=FLAG_KIND, target_locator=locator)
    return locator, current


def _picture_draft(worker: Any, options: ComfyOptions, subject: Subject, png: bytes) -> ProposalDraft:
    locator = {
        "channel": options.channel,
        "source_picture_ref": subject.picture_ref,
        "operation": worker.key,
        "variant": 1,
    }
    value = {"op": "derived", "operation": worker.key, "preset": "", "variant": 1, "of": 1}
    return ProposalDraft(PICTURE_KIND, locator, value, media_file=ContentFile(png, name=f"{worker.key}.png"))
