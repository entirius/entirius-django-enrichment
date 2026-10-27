# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Cost guard — a task whose known cost reached `ENRICHMENT_TASK_MAX_COST_USD` stops: its queued units are
cancelled (leased ones finish), the task records a plain error and the operator is notified once."""

from decimal import Decimal

from django.db import transaction

from django_enrichment.enums import TaskStatus, UnitStatus
from django_enrichment.models import EnrichmentTask
from django_enrichment.services import queue_config, queue_escalation, queue_log, task_progress_service, task_service

COST_LIMIT = "COST_LIMIT"
MESSAGE = "Stopped: this run reached its cost limit"


def max_cost() -> Decimal:
    return Decimal(str(queue_config.get("ENRICHMENT_TASK_MAX_COST_USD")))


def stop_over_cost(worker_key: str | None = None) -> int:
    """Stop every open task (of `worker_key` when given) over the limit that still has queued units."""
    tasks = EnrichmentTask.objects.filter(
        status__in=[TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value],
        cost_usd__gte=max_cost(),
        work_units__status=UnitStatus.QUEUED.value,
    ).distinct()
    if worker_key:
        tasks = tasks.filter(type=worker_key)
    stopped = 0
    for task in tasks:
        _stop(task)
        stopped += 1
    return stopped


def _stop(task: EnrichmentTask) -> None:
    with transaction.atomic():
        cancelled = task_progress_service.cancel_units(task)
        if cancelled:
            task_service.record_error(task, subject_ref="", code=COST_LIMIT, error_id="-", message=MESSAGE)
    if not cancelled:
        return
    queue_log.log("cost_limit", task=task.pk, worker=task.type, units=cancelled, code=COST_LIMIT)
    queue_escalation.notify(
        subject_ref=f"enrichment:task:{task.pk}",
        title="A run reached its cost limit",
        body=f"Task {task.pk} was stopped at {task.cost_usd} USD (limit {max_cost()} USD).",
    )
    task_progress_service.refresh(task.pk)
