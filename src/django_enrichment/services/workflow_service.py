# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Workflow runs (contract A4 § 6) — start, gates, approve → next steps, regenerate, cancel.

A run walks its frozen steps: every step whose needed steps are approved is spawned as a task
(`workflow_steps.spawn`); when the runner finishes it (`on_task_finished`) the step's proposal waits for
review — or is applied at once behind an `auto` gate. Approving = accepting that proposal through the
normal apply path, which calls `on_proposal_applied` → the step is approved, the ready steps spawn.
Every state change happens under the run's row lock. Approved answers go to memory through a seam.
"""

import logging

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from jsonschema import Draft202012Validator

from django_enrichment.enums import ProposalStatus, RunStatus, StepGate, StepOutput, StepStatus, TaskStatus
from django_enrichment.kinds import seams
from django_enrichment.models import ContentProposal, EnrichmentTask, WorkflowDefinition, WorkflowRun
from django_enrichment.services import workflow_steps

_logger = logging.getLogger("process")
_REGENERABLE = frozenset({StepStatus.REVIEW.value, StepStatus.FAILED.value})
_CLOSED = frozenset({RunStatus.DONE.value, RunStatus.CANCELLED.value})
CANCEL_REASON = "workflow run cancelled"


def start(*, definition: str, inputs: dict, channel: str = "", user: AbstractBaseUser | None = None) -> WorkflowRun:
    """Validate the brief against the definition's `inputs_schema`, create the run, spawn the first steps."""
    found = WorkflowDefinition.objects.filter(key=definition, active=True).first()
    if found is None:
        raise ValueError(f"unknown or inactive workflow {definition!r}")
    _validate_inputs(found.inputs_schema, inputs)
    with transaction.atomic():
        run = WorkflowRun.objects.create(
            definition=found,
            channel=channel,
            inputs=inputs,
            created_by=user,
            steps_state=workflow_steps.initial_state(found),
        )
        _spawn_ready(run)
        _save(run)
    return run


def _validate_inputs(schema: dict, inputs: dict) -> None:
    if errors := sorted(Draft202012Validator(schema).iter_errors(inputs), key=lambda err: list(err.path)):
        raise ValueError(
            "; ".join(f"inputs.{'.'.join(map(str, err.path)) or '(root)'}: {err.message}" for err in errors)
        )


def regenerate(uid: str, key: str, *, instruction: str = "") -> WorkflowRun:
    """A new version of a step that needs review or failed: its pending proposal is superseded."""
    with transaction.atomic():
        run = _locked(uid)
        step = workflow_steps.find(run, key)
        if step["status"] not in _REGENERABLE:
            raise ValueError(f"step {key!r} is {step['status']}; only a step in review or failed can be regenerated")
        previous = _supersede(step["proposal_id"])
        workflow_steps.spawn(run, step, instruction=instruction, previous=previous)
        _save(run)
    return run


def cancel(uid: str, *, user: AbstractBaseUser | None = None) -> WorkflowRun:
    """Stop the run: running step tasks are cancelled, the run's pending proposals rejected."""
    from django_enrichment.services import proposal_service  # local import: avoids service import cycle

    with transaction.atomic():
        run = _locked(uid)
        for step in run.steps_state:
            if step["status"] == StepStatus.RUNNING.value:
                _cancel_task(step["task_id"])
        run.status = RunStatus.CANCELLED.value
        _save(run)
        proposal_service.bulk_reject({"subject_ref": workflow_steps.subject_ref(run)}, CANCEL_REASON, user=user)
    return run


def on_task_finished(task: EnrichmentTask) -> None:
    """Runner completion hook: the step's proposal waits for review (or is applied behind an auto gate)."""
    if not (uid := (task.params or {}).get("run")):
        return
    with transaction.atomic():
        run = _open_run(uid)
        step = _step_of_task(run, task)
        if step is None:
            return  # cancelled run or a superseded attempt
        proposal = _output(task)
        step.update(status=StepStatus.REVIEW.value if proposal else StepStatus.FAILED.value)
        step["proposal_id"] = proposal.pk if proposal else None
        _save(run)
    if proposal is not None and step["gate"] == StepGate.AUTO.value:
        _auto_approve(proposal, uid, step["key"])


def on_proposal_applied(proposal: ContentProposal) -> None:
    """Apply hook (any `run:` proposal): approve its step, remember answers, spawn the steps now ready."""
    if not (uid := workflow_steps.run_uid(proposal.subject_ref)):
        return
    with transaction.atomic():
        run = _open_run(uid)
        step = next((item for item in run.steps_state if item["proposal_id"] == proposal.pk), None) if run else None
        if step is None:
            return
        step["status"] = StepStatus.APPROVED.value
        if proposal.result_ref:
            run.subject_ref = proposal.result_ref
        if step["output"] == StepOutput.QUESTIONS.value:
            seams.record_answers(run=run, step=step["key"], value=proposal.proposed_value)
        _spawn_ready(run)
        _save(run)


def _locked(uid: str) -> WorkflowRun:
    """The run under a row lock, still open. Raises `ValueError` (unknown → not found, closed → refused)."""
    run = WorkflowRun.objects.select_for_update().filter(uid=uid).first()
    if run is None:
        raise ValueError(f"workflow run {uid!r} not found")
    if run.status in _CLOSED:
        raise ValueError(f"workflow run {uid!r} is {run.status}")
    return run


def _open_run(uid: str) -> WorkflowRun | None:
    return WorkflowRun.objects.select_for_update().filter(uid=uid).exclude(status=RunStatus.CANCELLED).first()


def _step_of_task(run: WorkflowRun | None, task: EnrichmentTask) -> dict | None:
    if run is None:
        return None
    return next((step for step in run.steps_state if step["task_id"] == task.pk), None)


def _output(task: EnrichmentTask) -> ContentProposal | None:
    """The step's proposal — only from a task that finished well."""
    if task.status != TaskStatus.DONE.value:
        return None
    return task.proposals.filter(status=ProposalStatus.PENDING.value).order_by("-pk").first()


def _auto_approve(proposal: ContentProposal, uid: str, key: str) -> None:
    """Auto gate: apply at once; a refused apply leaves the step failed (the operator can regenerate it)."""
    from django_enrichment.services import apply_service  # local import: avoids service import cycle

    try:
        apply_service.apply(proposal)
    except Exception:  # noqa: BLE001 — an opaque adapter write must not crash the runner
        _logger.exception("auto approval of step %r in run %s failed", key, uid)
        _mark_failed(uid, key)


def _mark_failed(uid: str, key: str) -> None:
    with transaction.atomic():
        if run := _open_run(uid):
            workflow_steps.find(run, key)["status"] = StepStatus.FAILED.value
            _save(run)


def _supersede(proposal_id: int | None) -> dict | None:
    """Retire the step's pending proposal; its value is what the regeneration improves on."""
    from django_enrichment.services import proposal_service  # local import: avoids service import cycle

    proposal = ContentProposal.objects.filter(pk=proposal_id).first() if proposal_id else None
    if proposal is None:
        return None
    if proposal.status == ProposalStatus.PENDING.value:
        proposal_service.transition_status(proposal, ProposalStatus.SUPERSEDED.value)
    return proposal.proposed_value


def _cancel_task(task_id: int | None) -> None:
    from django_enrichment.services import task_service  # local import: avoids service import cycle

    task = EnrichmentTask.objects.filter(pk=task_id).first()
    try:
        if task is not None:
            task_service.transition_status(task, TaskStatus.CANCELLED.value)
    except ValueError:
        _logger.info("step task %s already finished; nothing to cancel", task_id)


def _spawn_ready(run: WorkflowRun) -> None:
    for step in workflow_steps.ready(run):
        workflow_steps.spawn(run, step)


def _save(run: WorkflowRun) -> None:
    run.status = workflow_steps.derive_status(run)
    workflow_steps.refresh_cost(run)
    run.save()
