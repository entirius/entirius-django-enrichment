# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Batch correction — one instruction / quick action over every pending text proposal of a task.

`queue` validates and hands the work to Celery (`enrichment.revise_task`, default queue like the
runner — AI calls block a worker); `revise_pending` runs one completion per proposal. A failing
proposal is recorded in `task.errors` (stable code, never exception text) and counted in
`counts.failed`; successes count in `counts.revised`. Expected outcomes (value not revisable, proposal
closed meanwhile, answer not applicable) log one info line; only unexpected errors log a traceback.
The field catalog is looked up once per (module, channel, language) per batch run.
"""

import functools
import logging
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.db.models import QuerySet

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import catalog_service, revise_prompt, revise_service, task_service, toolbox_calls
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
    run = {**correction, "user": user, "describe_fields": functools.lru_cache(catalog_service.describe_fields)}
    for proposal in _pending(task).iterator():
        _revise_one(task, proposal, run)


def _revise_one(task: EnrichmentTask, proposal: ContentProposal, run: dict) -> None:
    try:
        revise_service.revise(
            proposal,
            instruction=run.get("instruction", ""),
            quick_action=run.get("quick_action"),
            user=run["user"],
            describe_fields=run["describe_fields"],
        )
    except revise_service.RevisionNotApplicableError:
        _fail(task, proposal, "REVISION_NOT_APPLICABLE")
    except ValueError:
        _fail(task, proposal, "NOT_REVISABLE")
    except Exception as exc:  # noqa: BLE001 — per-proposal isolation: one failure must not stop the batch
        _fail(task, proposal, toolbox_calls.failure_code(exc), unexpected=True)
    else:
        task_service.increment_count(task, "revised")


def _fail(task: EnrichmentTask, proposal: ContentProposal, code: str, *, unexpected: bool = False) -> None:
    error_id = uuid.uuid4().hex[:12]
    log = _logger.exception if unexpected else _logger.info
    log("batch correction %s on proposal %s of task %s [%s]", code, proposal.pk, task.pk, error_id)
    task_service.record_error(task, subject_ref=proposal.subject_ref, code=code, error_id=error_id)
    task_service.increment_count(task, "failed")


def _pending(task: EnrichmentTask) -> QuerySet[ContentProposal]:
    """The task's pending text proposals (media proposals are not revisable)."""
    return task.proposals.filter(status=ProposalStatus.PENDING.value, staged_file="").exclude(
        target_kind__in=("picture", "video")
    )
