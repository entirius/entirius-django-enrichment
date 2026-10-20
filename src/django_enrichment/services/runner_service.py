# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Subject execution and intake for the work queue — what runs ONE subject and hands its drafts to intake.

`run_subject` is the internal consumer's step (kind → drafts, failures turned into a `SubjectResult` with a
stable code and an error id); `intake_subject` is shared by every worker's `complete` (internal or external):
supersede and keep-edited rules live in `proposal_service`, unchanged.
"""

import logging
import uuid

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds import Kind, ProposalDraft, extras
from django_enrichment.models import ContentProposal, EnrichmentTask, Worker
from django_enrichment.services import (
    proposal_service,
    qa_auto_check,
    queue_config,
    queue_log,
    task_service,
    toolbox_calls,
)
from django_enrichment.services.queue_types import LeaseLost, SubjectResult

_logger = logging.getLogger("process")


def run_subject(worker: Worker, kind: Kind, task: EnrichmentTask, subject_ref: str) -> SubjectResult:
    """Run the kind on one subject — unless the operator already edited a proposal of this worker on it: then
    nothing is generated (no paid call) and the subject counts in `counts.kept_edited`. A transient failure
    (`ENRICHMENT_TRANSIENT_ERROR_CODES`) is retried `ENRICHMENT_TRANSIENT_RETRIES` times; a failure is logged with
    an error id and returned, never raised."""
    prefix = f"worker:{worker.key}@"
    if proposal_service.subject_has_operator_edits(worker.target_module, subject_ref, prefix):
        task_service.increment_count(task, "kept_edited")
        return SubjectResult(subject_ref)
    retries = queue_config.get("ENRICHMENT_TRANSIENT_RETRIES")
    result = _attempt(worker, kind, task, subject_ref)
    while retries > 0 and result.error_code in queue_config.get("ENRICHMENT_TRANSIENT_ERROR_CODES"):
        queue_log.log("subject_retry", task=task.pk, worker=worker.key, code=result.error_code)
        retries -= 1
        result = _attempt(worker, kind, task, subject_ref)
    return result


def _attempt(worker: Worker, kind: Kind, task: EnrichmentTask, subject_ref: str) -> SubjectResult:
    try:
        return SubjectResult(subject_ref, drafts=kind.run(worker, task, subject_ref))
    except LeaseLost:  # the unit is someone else's now: stop, no subject failure (`lease_keepalive`)
        raise
    except Exception as exc:  # noqa: BLE001 — per-subject isolation: one failing subject must not stop the unit
        error_id = uuid.uuid4().hex[:12]
        detail = toolbox_calls.failure_detail(exc)
        _logger.exception("runner failed on subject %r of task %s [%s] %s", subject_ref, task.pk, error_id, detail)
        return SubjectResult(
            subject_ref, error_code=toolbox_calls.failure_code(exc), error_id=error_id, error_message=detail
        )


def intake_subject(worker: Worker, task: EnrichmentTask, subject_ref: str, drafts: list[ProposalDraft]) -> None:
    """Hand a subject's drafts to intake (a missing display label is looked up once per subject), then queue the
    automatic checklist check of the new proposals when the run asks for it (`qa_auto_check`)."""
    label = _subject_label(worker, task, subject_ref) if any(not d.subject_label for d in drafts) else ""
    proposals = [_intake(worker, task, subject_ref, draft, label) for draft in drafts]
    qa_auto_check.schedule(task, [p for p in proposals if p is not None])


def _subject_label(worker: Worker, task: EnrichmentTask, subject_ref: str) -> str:
    """The name review shows for a subject whose drafts carry none (a product's name next to its SKU)."""
    adapter = get_adapter(worker.target_module)
    return extras.subject_name(adapter, subject_ref=subject_ref, channel=task.params.get("channel", ""))


def _intake(
    worker: Worker, task: EnrichmentTask, subject_ref: str, draft: ProposalDraft, label: str
) -> ContentProposal | None:
    """Hand one draft to intake — `intake_media` (staged binary) when it carries a media file. A target whose
    pending proposal the operator edited keeps it (also when the edit lands while intake runs): the draft is
    dropped and counted in `counts.kept_edited`."""
    target = (worker.target_module, subject_ref, draft.target_kind, draft.target_locator)
    if proposal_service.has_operator_edits(*target):
        task_service.increment_count(task, "kept_edited")
        return None
    fields = {
        "target_module": worker.target_module,
        "subject_ref": subject_ref,
        "target_kind": draft.target_kind,
        "target_locator": draft.target_locator,
        "proposed_value": draft.proposed_value,
        "task": task,
        "subject_label": draft.subject_label or label,
        "source": f"worker:{worker.key}@v{worker.version}",
        "cost_usd": draft.cost_usd,
        "keep_edited": True,
    }
    try:
        if draft.media_file is None:
            return proposal_service.intake(**fields)
        return proposal_service.intake_media(file=draft.media_file, **fields)
    except proposal_service.KeptEdited:
        task_service.increment_count(task, "kept_edited")
        return None
