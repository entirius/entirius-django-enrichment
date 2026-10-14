# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Batch correction — one instruction / quick action over every pending text proposal of a task.

`queue` validates and hands the work to Celery (`enrichment.revise_task`, default queue like the
runner — AI calls block a worker); `revise_pending` runs one completion per proposal. A failing
proposal is logged under an error id, recorded in `task.errors` (stable code, never exception text)
and counted in `counts.failed`; successes count in `counts.revised`.
"""

import logging
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.db.models import QuerySet

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import revise_prompt, revise_service, task_service, toolbox_calls
from django_enrichment.services.revise_prompt import VARIANTS

_logger = logging.getLogger("process")


def queue(task: EnrichmentTask, correction: dict, *, user: AbstractBaseUser | None = None) -> int:
    """Queue the batch; returns how many proposals it covers. `correction` = `{instruction, quick_action}`."""
    if correction.get("quick_action") == VARIANTS:
        raise ValueError("variants cannot run as a batch correction")
    revise_prompt.expand_instruction(correction.get("instruction", ""), correction.get("quick_action"))
    count = _pending(task).count()
    if count:
        from django_enrichment.tasks.revise_tasks import revise_task

        revise_task.delay(task.pk, correction, getattr(user, "pk", None))
    return count


def revise_pending(task_id: int, correction: dict, *, user_id: int | None = None) -> None:
    task = task_service.get_task(task_id)
    user = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    for proposal in _pending(task).iterator():
        _revise_one(task, proposal, correction, user)


def _revise_one(task: EnrichmentTask, proposal: ContentProposal, correction: dict, user) -> None:
    try:
        revise_service.revise(
            proposal,
            instruction=correction.get("instruction", ""),
            quick_action=correction.get("quick_action"),
            user=user,
        )
    except Exception as exc:  # noqa: BLE001 — per-proposal isolation: one failure must not stop the batch
        error_id = uuid.uuid4().hex[:12]
        _logger.exception("batch correction failed on proposal %s of task %s [%s]", proposal.pk, task.pk, error_id)
        task_service.record_error(task, subject_ref=proposal.subject_ref, code=_code(exc), error_id=error_id)
        task_service.increment_count(task, "failed")
        return
    task_service.increment_count(task, "revised")


def _code(exc: Exception) -> str:
    if isinstance(exc, revise_service.RevisionNotApplicableError):
        return "REVISION_NOT_APPLICABLE"
    return toolbox_calls.failure_code(exc)


def _pending(task: EnrichmentTask) -> QuerySet[ContentProposal]:
    """The task's pending text proposals (media proposals are not revisable)."""
    return task.proposals.filter(status=ProposalStatus.PENDING.value, staged_file="").exclude(
        target_kind__in=("picture", "video")
    )
