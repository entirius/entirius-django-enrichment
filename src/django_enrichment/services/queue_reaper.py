# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Reaper (beat, every minute): expired leases go back to the queue or fail, stalled work is escalated,
consumers are restarted where work waits and nothing is leased (recovery after a crash or restart)."""

from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from django_enrichment.enums import TaskStatus, UnitStatus, WorkerMode
from django_enrichment.models import EnrichmentTask, Worker, WorkUnit
from django_enrichment.services import (
    cost_guard_service,
    queue_dispatch,
    queue_escalation,
    queue_log,
    task_progress_service,
    work_queue_service,
)

EXPIRED = "LEASE_EXPIRED"
_EXPIRED_MESSAGE = "The worker stopped answering; the work was put back in the queue."
_BATCH = 100
_UNPLANNED_AFTER_S = 120


def reap() -> dict[str, int]:
    summary = {
        "expired": _expire_leases(),
        "cost_stopped": cost_guard_service.stop_over_cost(),
        "stale_alerts": queue_escalation.check_stale_queue(),
        "kicked": _kick_consumers() + _kick_unplanned(),
    }
    queue_log.log("reap", **summary)
    return summary


def _expire_leases() -> int:
    expired = 0
    for pk in list(_expired_ids()):
        with transaction.atomic():
            unit = (
                WorkUnit.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, status=UnitStatus.LEASED.value)
                .first()
            )
            if unit is None or unit.lease_until >= timezone.now():
                continue
            queue_log.unit_event(unit, "lease_expired", code=EXPIRED)
            work_queue_service.requeue_or_fail(unit, code=EXPIRED, message=_EXPIRED_MESSAGE, retryable=True)
        task_progress_service.refresh(unit.task_id)
        expired += 1
    return expired


def _expired_ids():
    return WorkUnit.objects.filter(status=UnitStatus.LEASED.value, lease_until__lt=timezone.now()).values_list(
        "pk", flat=True
    )[:_BATCH]


def _kick_consumers() -> int:
    """One consumer per (internal running worker, lane) that has queued units and none leased."""
    leased = WorkUnit.objects.filter(
        worker_key=OuterRef("worker_key"), lane=OuterRef("lane"), status=UnitStatus.LEASED.value
    )
    pairs = (
        WorkUnit.objects.filter(status=UnitStatus.QUEUED.value)
        .annotate(busy=Exists(leased))
        .filter(busy=False)
        .values_list("worker_key", "lane")
        .distinct()
    )
    running = set(
        Worker.objects.filter(active=True, paused_reason="", mode=WorkerMode.INTERNAL.value).values_list(
            "key", flat=True
        )
    )
    kicked = [(key, lane) for key, lane in pairs if key in running]
    for key, lane in kicked:
        queue_dispatch.kick_consumer(key, lane)
    return len(kicked)


def _kick_unplanned() -> int:
    """Open tasks of an active worker that never got units (the planner crashed or its message was lost)."""
    cutoff = timezone.now() - timedelta(seconds=_UNPLANNED_AFTER_S)
    keys = Worker.objects.filter(active=True).values_list("key", flat=True)
    tasks = EnrichmentTask.objects.filter(status=TaskStatus.OPEN.value, type__in=keys, created_at__lt=cutoff)
    tasks = tasks.filter(~Exists(WorkUnit.objects.filter(task=OuterRef("pk"))))[:20]
    for task in tasks:
        queue_dispatch.kick_plan(task)
    return len(tasks)
