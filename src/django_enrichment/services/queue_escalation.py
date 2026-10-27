# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Queue escalation — pause a failing worker and tell the operator through django_notifications.

Soft dependency: the module absent, the channel missing or `notify` raising → one log line, never an error in
the queue. Every notification carries a stable `subject_ref` + title, so repeats are counted by the notifications
module's own dedup instead of spamming.
"""

import logging
from datetime import timedelta

from django.db.models import Min
from django.utils import timezone

from django_enrichment.enums import UnitStatus
from django_enrichment.models import Worker, WorkerInstance, WorkUnit
from django_enrichment.services import queue_config, queue_log

_SEVERITY = "high"


def notify(*, subject_ref: str, title: str, body: str) -> bool:
    """Raise a notification on the configured channel; False (and a log line) when it could not be raised."""
    try:
        from django_notifications.services import notify_service
    except (ImportError, RuntimeError):  # not installed, or installed but not in INSTALLED_APPS
        queue_log.log("notify_skipped", code="NOTIFICATIONS_ABSENT", subject=subject_ref)
        return False
    try:
        notify_service.notify(
            channel_idx=queue_config.get("ENRICHMENT_NOTIFY_CHANNEL"),
            recipient_role=queue_config.get("ENRICHMENT_NOTIFY_ROLE"),
            severity=_SEVERITY,
            subject_ref=subject_ref,
            title=title,
            body=body,
        )
    except Exception as exc:  # noqa: BLE001 — escalation must never break the queue (missing channel, db hiccup)
        queue_log.log("notify_skipped", logging.WARNING, code=type(exc).__name__, subject=subject_ref)
        return False
    return True


def unit_failed_for_good(unit: WorkUnit) -> None:
    """One notification per task; further failed units of it are counted as repeats."""
    worker = Worker.objects.filter(key=unit.worker_key).first()
    label = worker.label if worker else unit.worker_key
    body = f"{len(unit.subject_refs)} products could not be processed (task {unit.task_id}). Reason: {unit.last_error.get('message', '')}"
    notify(
        subject_ref=f"enrichment:task:{unit.task_id}",
        title=f"{label}: products could not be processed",
        body=body,
    )


def after_failure(worker_key: str, holder: str, code: str) -> None:
    """Pause the worker on a budget error or after too many failed units in a row on one instance."""
    if code in queue_config.get("ENRICHMENT_BUDGET_ERROR_CODES"):
        pause(worker_key, "The AI budget or spending limit is reached. Raise the limit, then resume.")
        return
    failures = (
        WorkerInstance.objects.filter(worker__key=worker_key, instance=holder)
        .values_list("consecutive_failures", flat=True)
        .first()
    )
    if failures and failures >= queue_config.get("ENRICHMENT_PAUSE_AFTER_FAILURES"):
        pause(worker_key, f"{failures} work units failed in a row. Check the cause, then resume.")


def pause(worker_key: str, reason: str) -> None:
    """Pause the worker (when auto-pause is on) and notify once; an already paused worker is left alone."""
    worker = Worker.objects.filter(key=worker_key).first()
    if worker is None or worker.paused_reason:
        return
    if queue_config.get("ENRICHMENT_AUTO_PAUSE"):
        Worker.objects.filter(pk=worker.pk, paused_reason="").update(paused_reason=reason, paused_at=timezone.now())
    queue_log.log("worker_paused", logging.WARNING, worker=worker_key, auto=queue_config.get("ENRICHMENT_AUTO_PAUSE"))
    notify(subject_ref=f"enrichment:worker:{worker_key}", title=f"{worker.label}: work paused", body=reason)


def check_stale_queue() -> int:
    """Notify for every running worker with work waiting too long and nobody online. Returns how many."""
    now = timezone.now()
    stale_before = now - timedelta(seconds=queue_config.get("ENRICHMENT_STALE_QUEUE_S"))
    online_since = now - timedelta(seconds=queue_config.get("ENRICHMENT_INSTANCE_ONLINE_S"))
    waiting = (
        WorkUnit.objects.filter(status=UnitStatus.QUEUED.value)
        .values("worker_key")
        .annotate(oldest=Min("created_at"))
        .filter(oldest__lt=stale_before)
    )
    raised = 0
    for row in waiting:
        worker = Worker.objects.filter(key=row["worker_key"], active=True, paused_reason="").first()
        online = WorkerInstance.objects.filter(worker=worker, last_seen__gte=online_since).exists() if worker else True
        if worker is not None and not online:
            raised += notify(
                subject_ref=f"enrichment:worker:{worker.key}",
                title=f"Nobody is taking {worker.label} work",
                body="Work has been waiting in the queue and no worker instance has asked for it recently.",
            )
    return raised
