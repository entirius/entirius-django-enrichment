# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Complete a leased unit: the reported results go through proposal intake (supersede / keep-edited rules
unchanged), the unit becomes done, the task's status is re-derived. Idempotent per lease token."""

import logging
import uuid

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from django_enrichment.enums import TaskStatus, UnitStatus
from django_enrichment.models import EnrichmentTask, Worker, WorkerInstance, WorkUnit
from django_enrichment.services import queue_log, runner_service, task_progress_service, task_service
from django_enrichment.services.queue_types import LeaseLost, SubjectResult
from django_enrichment.services.work_queue_service import locked_unit

_logger = logging.getLogger("process")
NO_RESULT = "NO_RESULT"
INTAKE_FAILED = "INTAKE_FAILED"


def complete(token: uuid.UUID | str, results: list[SubjectResult]) -> dict:
    """Store the results of the unit holding `token`; returns the outcome `{succeeded, failed, kept_edited}`.

    A repeat with the same token answers the stored outcome. A token that no longer holds the lease raises
    `LeaseLost` (results discarded). A task cancelled before this call discards the results too (`discarded`).
    Raises `ValueError` for a result naming a subject the unit does not carry.
    """
    with transaction.atomic():
        unit = locked_unit(token)
        if unit.status == UnitStatus.DONE.value:
            return unit.outcome
        if unit.status != UnitStatus.LEASED.value:
            raise LeaseLost("not leased")
        by_ref = _by_ref(unit, results)
        outcome = _settle(unit, by_ref)
    task_progress_service.refresh(unit.task_id)
    return outcome


def _by_ref(unit: WorkUnit, results: list[SubjectResult]) -> dict[str, SubjectResult]:
    unknown = {r.subject_ref for r in results} - set(unit.subject_refs)
    if unknown:
        raise ValueError(f"results name subjects outside the unit: {sorted(unknown)}")
    return {r.subject_ref: r for r in results}


def _settle(unit: WorkUnit, by_ref: dict[str, SubjectResult]) -> dict:
    task = EnrichmentTask.objects.get(pk=unit.task_id)
    if task.status == TaskStatus.CANCELLED.value:
        outcome = {"discarded": True, "succeeded": 0, "failed": 0, "kept_edited": 0}
        unit.status = UnitStatus.CANCELLED.value
        queue_log.unit_event(unit, "discarded", code="TASK_CANCELLED")
    else:
        outcome = _intake_all(unit, task, by_ref)
        unit.status = UnitStatus.DONE.value
        queue_log.unit_event(unit, "completed", detail=_detail(outcome))
        _count_done(unit)
    unit.outcome = outcome
    unit.save()
    return outcome


def _detail(outcome: dict) -> str:
    return f"ok={outcome['succeeded']} failed={outcome['failed']} kept_edited={outcome['kept_edited']}"


def _count_done(unit: WorkUnit) -> None:
    WorkerInstance.objects.filter(worker__key=unit.worker_key, instance=unit.leased_by).update(
        units_done=F("units_done") + 1, consecutive_failures=0, last_seen=timezone.now()
    )


def _intake_all(unit: WorkUnit, task: EnrichmentTask, by_ref: dict[str, SubjectResult]) -> dict:
    worker = Worker.objects.get(key=unit.worker_key)
    outcome = {"succeeded": 0, "failed": 0, "kept_edited": 0}
    before = task.counts.get("kept_edited", 0)
    for ref in unit.subject_refs:
        result = by_ref.get(ref) or SubjectResult(ref, error_code=NO_RESULT)
        if _intake_one(worker, task, result):
            outcome["succeeded"] += 1
        else:
            outcome["failed"] += 1
    task.refresh_from_db(fields=["counts"])
    outcome["kept_edited"] = task.counts.get("kept_edited", 0) - before
    return outcome


def _intake_one(worker: Worker, task: EnrichmentTask, result: SubjectResult) -> bool:
    """True when the subject's drafts were taken; a failed subject is counted and recorded, never raised."""
    code, error_id = result.error_code, result.error_id
    if not code:
        try:
            with transaction.atomic():
                runner_service.intake_subject(worker, task, result.subject_ref, result.drafts)
            return True
        except Exception:  # noqa: BLE001 — per-subject isolation: one bad draft must not lose the unit
            error_id = uuid.uuid4().hex[:12]
            code = INTAKE_FAILED
            _logger.exception(
                "queue intake failed for subject %r of task %s [%s]", result.subject_ref, task.pk, error_id
            )
    task_service.increment_count(task, "failed")
    task_service.record_error(
        task,
        subject_ref=result.subject_ref,
        code=code,
        error_id=error_id or "-",
        message=result.error_message,
        exception=result.error_exception,
    )
    return False
