# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Internal consumer — pulls units through the same `work_queue_service` an external worker uses.

`consume` handles a few units one after the other (a Celery slot is never held for long), subject by subject,
with a heartbeat before each subject and, inside a long subject, between its toolbox calls (`lease_keepalive`).
A budget error stops the unit and pauses the worker; a lost lease stops the
consumer at once; a cancelled task stops it before the next paid call.
"""

import logging
import os
import socket
import time

from django_enrichment.enums import TaskStatus, WorkerMode
from django_enrichment.kinds import extras, get_kind
from django_enrichment.models import EnrichmentTask, Worker, WorkUnit
from django_enrichment.services import (
    lease_keepalive,
    queue_config,
    queue_log,
    runner_service,
    unit_result_service,
    work_queue_service,
)
from django_enrichment.services.queue_types import LeaseLost, SubjectResult

_logger = logging.getLogger("process")
_PLAIN = {"INTERNAL_ERROR": "The worker hit an internal error. See the log for the unit id."}


class BudgetStop(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def consume(worker_key: str, lane: str) -> int:
    """Handle up to `ENRICHMENT_CONSUME_MAX_UNITS` units of (worker, lane). Returns how many were handled."""
    worker = Worker.objects.filter(key=worker_key, active=True, mode=WorkerMode.INTERNAL.value).first()
    if worker is None:
        return 0
    handled = 0
    while handled < queue_config.get("ENRICHMENT_CONSUME_MAX_UNITS"):
        unit = work_queue_service.claim(worker_key, instance_id(), str(worker.version), lane)
        if unit is None:
            break
        handled += 1
        if not process_unit(worker, unit):
            break
    return handled


def has_more(worker_key: str, lane: str) -> bool:
    return WorkUnit.objects.filter(worker_key=worker_key, lane=lane, status="queued").exists()


def process_unit(worker: Worker, unit: WorkUnit) -> bool:
    """Run one leased unit and complete or fail it. False = stop consuming (lease lost)."""
    token, started = unit.lease_token, time.monotonic()
    extras.clear_cache()
    try:
        results = _run_subjects(worker, unit)
        unit_result_service.complete(token, results)
    except LeaseLost:
        queue_log.log("lease_lost", unit=unit.pk, task=unit.task_id, worker=unit.worker_key, code="CONSUMER")
        return False
    except BudgetStop as stop:
        work_queue_service.fail(token, stop.code, "The AI budget or spending limit is reached.", retryable=True)
    except Exception:  # noqa: BLE001 — a unit-level crash must requeue the unit, not kill the consumer
        _logger.exception("consumer failed on unit %s of task %s", unit.pk, unit.task_id)
        work_queue_service.fail(token, "INTERNAL_ERROR", _PLAIN["INTERNAL_ERROR"], retryable=True)
    finally:
        extras.clear_cache()
    queue_log.log("unit_handled", unit=unit.pk, duration_ms=int((time.monotonic() - started) * 1000))
    return True


def _run_subjects(worker: Worker, unit: WorkUnit) -> list[SubjectResult]:
    kind = get_kind(worker.kind)()
    task = EnrichmentTask.objects.get(pk=unit.task_id)
    results: list[SubjectResult] = []
    with lease_keepalive.kept_alive(lambda: work_queue_service.heartbeat(unit.lease_token)):
        for ref in unit.subject_refs:
            work_queue_service.heartbeat(unit.lease_token)
            task.refresh_from_db(fields=["status"])
            if task.status == TaskStatus.CANCELLED.value:
                break
            result = runner_service.run_subject(worker, kind, task, ref)
            if result.error_code in queue_config.get("ENRICHMENT_BUDGET_ERROR_CODES"):
                raise BudgetStop(result.error_code)
            results.append(result)
    return results
