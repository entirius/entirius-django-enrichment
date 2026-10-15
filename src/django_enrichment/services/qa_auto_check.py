# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Automatic checklist check (`qa_checklist.check_standard`) of the text proposals a run produces.

A run decides with `params.qa_check` (`true` | `false`); without it the instance default applies
(`profile_service.qa_check_default`, CMS Settings). Intake hands the new proposals to `schedule`, which queues one
Celery job per subject on the task's lane after commit; the completion charges the task (`toolbox_calls`). A
proposal that cannot be checked (no checklists, no longer pending, value changed, toolbox failure) stays unchecked —
the card then offers the manual check.
"""

import logging

from django.db import transaction

from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import lane_service, profile_service, qa_service

_logger = logging.getLogger("process")
QA_CHECK = "qa_check"


def carried_params(params: dict) -> dict:
    """`{"qa_check": bool}` when `params` says it, else `{}` (the instance default applies). Raises `ValueError`."""
    if QA_CHECK not in params:
        return {}
    if not isinstance(value := params[QA_CHECK], bool):
        raise ValueError("params invalid: qa_check: must be true or false")
    return {QA_CHECK: value}


def enabled_for(task: EnrichmentTask) -> bool:
    value = (task.params or {}).get(QA_CHECK)
    return value if isinstance(value, bool) else profile_service.qa_check_default()


def schedule(task: EnrichmentTask, proposals: list[ContentProposal]) -> None:
    """Queue the check of the new text proposals (not a workflow step's) when the task's run asks for it."""
    from django_enrichment.services.workflow_steps import run_uid

    ids = [p.pk for p in proposals if qa_service.text_of(p.proposed_value) and run_uid(p.subject_ref) is None]
    if not ids or not enabled_for(task):
        return
    from django_enrichment.tasks.qa_tasks import qa_check_task

    queue = lane_service.celery_queue(task.lane)
    transaction.on_commit(lambda: qa_check_task.apply_async(args=[ids], queue=queue))


def check_all(proposal_ids: list[int]) -> None:
    """Check each proposal still waiting for review; a failure leaves that one unchecked and is logged."""
    from django_enrichment.services import qa_checklist

    for proposal in ContentProposal.objects.filter(pk__in=proposal_ids, standard_checked_at__isnull=True):
        try:
            qa_checklist.check_standard(proposal)
        except Exception as exc:  # noqa: BLE001 — one proposal's failure must not stop the others
            _logger.warning("automatic checklist check skipped for proposal %s: %s", proposal.pk, exc)
