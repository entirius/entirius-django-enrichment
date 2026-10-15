# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Task progress derived from its units: open → in_progress at the first lease, done/failed when every unit
settled, cancelled with its queued units. Failed units land in the task's counts and errors."""

import logging

from django.db import transaction
from django.db.models import Count

from django_enrichment.enums import TaskStatus, UnitStatus
from django_enrichment.models import EnrichmentTask, WorkUnit
from django_enrichment.services import queue_log, task_service

_logger = logging.getLogger("process")
_RUNNING = (UnitStatus.QUEUED.value, UnitStatus.LEASED.value)


def mark_started(task_id: int) -> None:
    EnrichmentTask.objects.filter(pk=task_id, status=TaskStatus.OPEN.value).update(status=TaskStatus.IN_PROGRESS.value)


def refresh(task_id: int) -> None:
    """Re-derive the task's status after one of its units changed; a gap task asks the planner for more first."""
    with transaction.atomic():
        task = EnrichmentTask.objects.select_for_update().get(pk=task_id)
        if task.status in (TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value):
            finish_if_settled(task)


def finish_if_settled(task: EnrichmentTask) -> None:
    """Close the task once no unit is queued or leased (and a gap task has nothing new to plan)."""
    if task.units.filter(status__in=_RUNNING).exists() or _replanned(task):
        return
    totals = _totals(task)
    status = TaskStatus.DONE if totals["succeeded"] or not totals["problems"] else TaskStatus.FAILED
    try:
        if task.status == TaskStatus.OPEN.value:
            task_service.mark_in_progress(task)
        task_service.transition_status(task, status.value)
    except ValueError:
        _logger.info("queue left task %s as %s (changed meanwhile)", task.pk, task.status)
        return
    queue_log.log("task_settled", task=task.pk, status=status.value, **totals)
    _notify_workflow(task)


def _replanned(task: EnrichmentTask) -> bool:
    if task.scope_spec.get("mode") != "gap" or not task.units.exists():
        return False
    from django_enrichment.services import queue_dispatch, unit_planner

    if not unit_planner.plan_units(task):
        return False
    queue_dispatch.kick_consumer(task.type, task.lane)
    return True


def _totals(task: EnrichmentTask) -> dict[str, int]:
    done = task.units.filter(status=UnitStatus.DONE.value).values_list("outcome", flat=True)
    succeeded = sum(outcome.get("succeeded", 0) for outcome in done)
    failed_subjects = sum(outcome.get("failed", 0) for outcome in done)
    failed_units = task.units.filter(status=UnitStatus.FAILED.value).count()
    return {"succeeded": succeeded, "problems": failed_subjects + failed_units}


def _notify_workflow(task: EnrichmentTask) -> None:
    """A workflow step's task finished → its run moves on. Never fails the queue."""
    from django_enrichment.services import workflow_service

    try:
        workflow_service.on_task_finished(task)
    except Exception:  # noqa: BLE001 — the task itself is finished; a run hook must not undo that
        _logger.exception("workflow hook failed for task %s", task.pk)


def record_unit_failure(unit: WorkUnit) -> None:
    """A unit failed for good: its subjects count as failed in the task, with the unit's error code."""
    task = unit.task
    task_service.increment_count(task, "failed", len(unit.subject_refs))
    for ref in unit.subject_refs:
        task_service.record_error(
            task, subject_ref=ref, code=unit.last_error.get("code", ""), error_id=f"unit-{unit.pk}"
        )


def cancel_units(task: EnrichmentTask) -> int:
    """The task was closed (cancelled by the operator): queued units are cancelled, leased ones finish on their own."""
    cancelled = task.units.filter(status=UnitStatus.QUEUED.value).update(status=UnitStatus.CANCELLED.value)
    if cancelled:
        queue_log.log("units_cancelled", task=task.pk, units=cancelled)
    return cancelled


def unit_summary(task_ids: list[int]) -> dict[int, dict[str, int]]:
    """{task id: {unit status: n}} — one grouped query."""
    summary: dict[int, dict[str, int]] = {task_id: {} for task_id in task_ids}
    rows = WorkUnit.objects.filter(task_id__in=task_ids).values("task_id", "status").annotate(n=Count("id")).order_by()
    for row in rows:
        summary[row["task_id"]][row["status"]] = row["n"]
    return summary


def failed_units(task_id: int, limit: int = 5) -> list[dict]:
    """The latest failed units of a task with their error and last events — what the Runs drawer explains."""
    units = WorkUnit.objects.filter(task_id=task_id, status=UnitStatus.FAILED.value).order_by("-modified_at")[:limit]
    return [
        {
            "id": u.pk,
            "subject_refs": u.subject_refs,
            "attempts": u.attempts,
            "error": u.last_error,
            "events": u.events[-5:],
        }
        for u in units
    ]
