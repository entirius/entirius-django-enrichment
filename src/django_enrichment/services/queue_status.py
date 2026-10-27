# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Queue status for the CMS: one row per active worker, and the resume action."""

from datetime import timedelta

from django.db.models import Count, Min
from django.utils import timezone

from django_enrichment.enums import UnitStatus, WorkerMode
from django_enrichment.models import Worker, WorkerInstance, WorkUnit
from django_enrichment.services import queue_config, queue_dispatch, queue_log


def workers_status() -> list[dict]:
    """Per active worker: running/paused (+reason), online instances, queued, oldest queued age, leased, failed 24 h."""
    now = timezone.now()
    units = _unit_counts()
    oldest = dict(
        WorkUnit.objects.filter(status=UnitStatus.QUEUED.value)
        .values_list("worker_key")
        .annotate(oldest=Min("created_at"))
        .order_by()
    )
    online = _online_counts(now)
    failed = dict(
        WorkUnit.objects.filter(status=UnitStatus.FAILED.value, modified_at__gte=now - timedelta(hours=24))
        .values_list("worker_key")
        .annotate(n=Count("id"))
        .order_by()
    )
    return [
        _row(w, now, units.get(w.key, {}), oldest.get(w.key), online.get(w.key, 0), failed.get(w.key, 0))
        for w in _workers()
    ]


def _workers():
    return Worker.objects.filter(active=True).order_by("key")


def _unit_counts() -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for key, status, n in WorkUnit.objects.values_list("worker_key", "status").annotate(n=Count("id")).order_by():
        counts.setdefault(key, {})[status] = n
    return counts


def _online_counts(now) -> dict[str, int]:
    since = now - timedelta(seconds=queue_config.get("ENRICHMENT_INSTANCE_ONLINE_S"))
    rows = WorkerInstance.objects.filter(last_seen__gte=since).values_list("worker__key").annotate(n=Count("id"))
    return dict(rows.order_by())


def _row(worker: Worker, now, counts: dict[str, int], oldest, online: int, failed_24h: int) -> dict:
    queued = counts.get(UnitStatus.QUEUED.value, 0)
    stale = timedelta(seconds=queue_config.get("ENRICHMENT_STALE_QUEUE_S"))
    age = int((now - oldest).total_seconds()) if oldest else None
    return {
        "key": worker.key,
        "label": worker.label,
        "mode": worker.mode,
        "state": "paused" if worker.paused_reason else "running",
        "paused_reason": worker.paused_reason,
        "paused_at": worker.paused_at,
        "online_instances": online,
        "queued_units": queued,
        "oldest_queued_age_s": age,
        "leased_units": counts.get(UnitStatus.LEASED.value, 0),
        "failed_units_24h": failed_24h,
        "nobody_taking": bool(queued and not online and age is not None and age > stale.total_seconds()),
    }


def resume(worker_key: str) -> Worker:
    """Clear the pause (and the failure streaks that caused it); an internal worker's consumers are restarted."""
    worker = Worker.objects.filter(key=worker_key).first()
    if worker is None:
        raise ValueError(f"Worker {worker_key} not found")
    Worker.objects.filter(pk=worker.pk).update(paused_reason="", paused_at=None)
    WorkerInstance.objects.filter(worker=worker).update(consecutive_failures=0)
    queue_log.log("worker_resumed", worker=worker_key)
    worker.refresh_from_db()
    if worker.mode == WorkerMode.INTERNAL.value:
        for lane in (
            WorkUnit.objects.filter(worker_key=worker_key, status="queued").values_list("lane", flat=True).distinct()
        ):
            queue_dispatch.kick_consumer(worker_key, lane)
    return worker
