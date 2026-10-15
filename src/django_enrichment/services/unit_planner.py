# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit planner — splits a task's targets into small `WorkUnit`s, page by page (never all targets in memory).

`plan_task` runs in Celery under a row lock on the task, in one transaction: a crash plans nothing, a repeat
plans only what is missing. Gap-mode tasks are re-planned by `task_progress_service` when their units run out
(the gap queue is re-pulled from page 1 until it brings nothing new).
"""

import logging
import uuid
from collections.abc import Iterator
from itertools import islice
from typing import Any

from django.db import transaction

from django_enrichment.enums import TaskStatus, WorkerMode
from django_enrichment.models import EnrichmentTask, Worker, WorkUnit
from django_enrichment.services import (
    queue_config,
    queue_dispatch,
    queue_log,
    task_progress_service,
    task_service,
    toolbox_calls,
)

_logger = logging.getLogger("process")


def plan_task(task_id: int) -> int:
    """Plan the units of an open task, finish an empty one, start the consumer. Returns the units created.

    A task whose targets cannot be resolved fails (stable code + error id in `task.errors`, trace in the log)."""
    try:
        return _plan(task_id)
    except Exception as exc:  # noqa: BLE001 — a resolve failure must not leave the task open forever
        _fail_task(task_id, exc)
        return 0


def _plan(task_id: int) -> int:
    with transaction.atomic():
        task = EnrichmentTask.objects.select_for_update().get(pk=task_id)
        worker = Worker.objects.filter(key=task.type, active=True).first()
        if worker is None or task.status != TaskStatus.OPEN.value or (task.work_units.exists() and not task.batch_key):
            return 0
        created = plan_units(task)
        queue_log.log("planned", task=task.pk, worker=task.type, lane=task.lane, units=created)
        if created == 0 and not task.work_units.exists():
            task_progress_service.finish_if_settled(task)
    if created and worker.mode == WorkerMode.INTERNAL.value:
        queue_dispatch.kick_consumer(worker.key, task.lane)
    return created


def _fail_task(task_id: int, exc: Exception) -> None:
    error_id = uuid.uuid4().hex[:12]
    _logger.exception("planning failed for task %s [%s]", task_id, error_id)
    task = task_service.get_task(task_id)
    task_service.record_error(task, subject_ref="", code=toolbox_calls.failure_code(exc), error_id=error_id)
    if task.status == TaskStatus.OPEN.value:
        task_service.mark_in_progress(task)
        task_service.transition_status(task, TaskStatus.FAILED.value)


def plan_units(task: EnrichmentTask) -> int:
    """Create units for the subjects the task has no unit for yet. The caller holds the task row lock."""
    seen = {ref for refs in task.work_units.values_list("subject_refs", flat=True) for ref in refs}
    size, batch, created = queue_config.get("ENRICHMENT_UNIT_SIZE"), queue_config.get("ENRICHMENT_PLAN_BATCH"), 0
    room = max(queue_config.get("ENRICHMENT_QUEUE_MAX_SUBJECTS") - len(seen), 0)
    chunks = _chunks(islice(_fresh_subjects(task, seen), room), size)
    while rows := [_unit(task, refs) for refs in islice(chunks, batch)]:
        WorkUnit.objects.bulk_create(rows)
        created += len(rows)
    return created


def _unit(task: EnrichmentTask, refs: list[str]) -> WorkUnit:
    return WorkUnit(task=task, worker_key=task.type, lane=task.lane, subject_refs=refs)


def _chunks(refs: Iterator[str], size: int) -> Iterator[list[str]]:
    while chunk := list(islice(refs, size)):
        yield chunk


def _fresh_subjects(task: EnrichmentTask, seen: set[str]) -> Iterator[str]:
    """Distinct subject refs not yet planned, page by page. Gap mode is a work queue (always page 1); the
    seen-set ends the loop when a page brings nothing new (failed gap subjects stay in the queue)."""
    page = 1
    while fresh := _fresh_refs(task_service.resolve_targets(task, page=page), seen):
        seen.update(fresh)
        yield from fresh
        if task.scope_spec.get("mode") != "gap":
            page += 1


def _fresh_refs(targets: list[Any], seen: set[str]) -> list[str]:
    refs = [target["subject_ref"] if isinstance(target, dict) else str(target) for target in targets]
    return list(dict.fromkeys(ref for ref in refs if ref not in seen))
