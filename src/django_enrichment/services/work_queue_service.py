# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Work queue — claim a unit under a lease, heartbeat it, fail it. `unit_result_service` completes it.

All state changes run under a row lock (`select_for_update`); a claim skips locked rows, so two workers never
get the same unit. Every transition is logged through `queue_log` (`enrichment.queue`).
"""

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.utils import timezone

from django_enrichment.enums import TaskLane, TaskStatus, UnitStatus
from django_enrichment.models import Worker, WorkerInstance, WorkUnit
from django_enrichment.services import (
    cost_guard_service,
    queue_config,
    queue_escalation,
    queue_log,
    task_progress_service,
)
from django_enrichment.services.queue_types import LeaseLost

_CLOSED_TASKS = (TaskStatus.CANCELLED.value, TaskStatus.DONE.value, TaskStatus.FAILED.value)


def claim(worker_key: str, instance: str, version: str = "", lane: str = "") -> WorkUnit | None:
    """One queued unit leased to `instance`, or None (worker paused, in-flight cap reached, nothing to do).

    Raises `ValueError("... not found")` for an unknown or inactive worker. `lane` limits the claim to one lane;
    blank = any, interactive first. The in-flight cap counts the worker's leases (in `lane` when given).
    """
    now = timezone.now()
    cost_guard_service.stop_over_cost(worker_key)
    with transaction.atomic():
        worker = Worker.objects.select_for_update().filter(key=worker_key, active=True).first()
        if worker is None:
            raise ValueError(f"Worker {worker_key} not found")
        touch_instance(worker, instance, version)
        if (unit := _claimable(worker, lane, now)) is None:
            return None
        _lease(unit, instance, now)
    task_progress_service.mark_started(unit.task_id)
    return unit


def _claimable(worker: Worker, lane: str, now) -> WorkUnit | None:
    if worker.paused_reason:
        queue_log.log("claim_refused", worker=worker.key, code="PAUSED")
        return None
    held = WorkUnit.objects.filter(worker_key=worker.key, status=UnitStatus.LEASED.value)
    if lane:
        held = held.filter(lane=lane)
    if held.count() >= queue_config.get("ENRICHMENT_MAX_INFLIGHT_PER_WORKER"):
        queue_log.log("claim_refused", worker=worker.key, code="IN_FLIGHT_CAP")
        return None
    return _next_unit(worker.key, lane, now)


def _next_unit(worker_key: str, lane: str, now) -> WorkUnit | None:
    units = WorkUnit.objects.select_for_update(skip_locked=True, of=("self",)).filter(
        Q(not_before__isnull=True) | Q(not_before__lte=now),
        status=UnitStatus.QUEUED.value,
        worker_key=worker_key,
    )
    if lane:
        units = units.filter(lane=lane)
    interactive_first = Case(
        When(lane=TaskLane.NOW.value, then=Value(0)), default=Value(1), output_field=IntegerField()
    )
    units = units.exclude(task__cost_usd__gte=cost_guard_service.max_cost())
    return units.exclude(task__status__in=_CLOSED_TASKS).order_by(interactive_first, "created_at").first()


def _lease(unit: WorkUnit, instance: str, now) -> None:
    unit.status = UnitStatus.LEASED.value
    unit.lease_token = uuid.uuid4()
    unit.leased_by = instance
    unit.lease_until = now + timedelta(seconds=queue_config.get("ENRICHMENT_LEASE_S"))
    unit.not_before = None
    unit.attempts += 1
    queue_log.unit_event(unit, "claimed", instance=instance)
    unit.save()


def touch_instance(worker: Worker, instance: str, version: str = "") -> WorkerInstance:
    row, _ = WorkerInstance.objects.get_or_create(
        worker=worker, instance=instance, defaults={"last_seen": timezone.now(), "version": version[:32]}
    )
    row.last_seen, row.version = timezone.now(), version[:32] or row.version
    row.save(update_fields=["last_seen", "version", "modified_at"])
    return row


def locked_unit(token: uuid.UUID | str) -> WorkUnit:
    """The unit holding `token`, row-locked (inside the caller's transaction). Raises `LeaseLost` when none does."""
    try:
        unit = WorkUnit.objects.select_for_update().filter(lease_token=token).first()
    except (ValidationError, ValueError) as exc:  # a malformed token is simply not a lease
        raise LeaseLost("unknown token") from exc
    if unit is None:
        raise LeaseLost("unknown token")
    return unit


def heartbeat(token: uuid.UUID | str) -> WorkUnit:
    """Extend the lease by one lease length. A lost or expired lease raises `LeaseLost` — stop working."""
    now = timezone.now()
    with transaction.atomic():
        unit = locked_unit(token)
        if unit.status != UnitStatus.LEASED.value or unit.lease_until < now:
            queue_log.log("lease_lost", unit=unit.pk, task=unit.task_id, worker=unit.worker_key, code="HEARTBEAT")
            raise LeaseLost("lease expired")
        unit.lease_until = now + timedelta(seconds=queue_config.get("ENRICHMENT_LEASE_S"))
        unit.save(update_fields=["lease_until", "modified_at"])
        WorkerInstance.objects.filter(worker__key=unit.worker_key, instance=unit.leased_by).update(last_seen=now)
    return unit


def fail(token: uuid.UUID | str, code: str, message: str, retryable: bool = True) -> WorkUnit:
    """The worker could not finish the unit. Retryable and attempts left → queued again after a backoff; else failed."""
    with transaction.atomic():
        unit = locked_unit(token)
        if unit.status == UnitStatus.FAILED.value:
            return unit
        if unit.status != UnitStatus.LEASED.value:
            raise LeaseLost("not leased")
        holder = unit.leased_by
        requeue_or_fail(unit, code=code, message=message, retryable=retryable)
        _count_failure(unit.worker_key, holder, code)
    task_progress_service.refresh(unit.task_id)
    return unit


def requeue_or_fail(unit: WorkUnit, *, code: str, message: str, retryable: bool) -> None:
    """Put a leased unit back (backoff × attempt) or fail it for good; escalates a final failure."""
    unit.last_error = {"code": code, "message": message[:300]}
    holder = unit.leased_by
    if retryable and unit.attempts < queue_config.get("ENRICHMENT_MAX_ATTEMPTS"):
        backoff = queue_config.get("ENRICHMENT_RETRY_BACKOFF_S") * unit.attempts
        unit.status, unit.not_before = UnitStatus.QUEUED.value, timezone.now() + timedelta(seconds=backoff)
        queue_log.unit_event(unit, "requeued", instance=holder, code=code, detail=f"retry in {backoff}s")
    else:
        unit.status = UnitStatus.FAILED.value
        queue_log.unit_event(unit, "failed", instance=holder, code=code, detail=message[:120])
    if unit.status == UnitStatus.QUEUED.value:
        unit.lease_token, unit.leased_by, unit.lease_until = None, "", None
    unit.save()
    if unit.status == UnitStatus.FAILED.value:
        task_progress_service.record_unit_failure(unit)
        queue_escalation.unit_failed_for_good(unit)


def _count_failure(worker_key: str, holder: str, code: str) -> None:
    """Count the failure on the instance that held the unit; may pause the worker (`queue_escalation`)."""
    rows = WorkerInstance.objects.filter(worker__key=worker_key, instance=holder)
    if rows.update(units_failed=F("units_failed") + 1, consecutive_failures=F("consecutive_failures") + 1):
        queue_escalation.after_failure(worker_key, holder, code)
