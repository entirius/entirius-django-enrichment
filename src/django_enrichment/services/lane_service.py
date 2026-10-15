# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Run lanes — which Celery queue runs an internal task, and how many runs wait ahead of it.

`now` (interactive lane) is for a click the operator waits on: one product, a workflow step, a correction.
`queue` (bulk lane) is for a selection; its worker runs one task at a time, so a bulk run never blocks a click.
"""

from bisect import bisect_left
from collections import defaultdict

from django.conf import settings

from django_enrichment import settings as enrichment_settings
from django_enrichment.enums import TaskLane, TaskStatus, WorkerMode
from django_enrichment.models import EnrichmentTask, Worker


def interactive_queue() -> str:
    return getattr(settings, "ENRICHMENT_INTERACTIVE_QUEUE", enrichment_settings.ENRICHMENT_INTERACTIVE_QUEUE)


def bulk_queue() -> str:
    return getattr(settings, "ENRICHMENT_BULK_QUEUE", enrichment_settings.ENRICHMENT_BULK_QUEUE)


def celery_queue(lane: str) -> str:
    return interactive_queue() if lane == TaskLane.NOW.value else bulk_queue()


def run_now_max() -> int:
    return getattr(settings, "ENRICHMENT_RUN_NOW_MAX_SUBJECTS", enrichment_settings.ENRICHMENT_RUN_NOW_MAX_SUBJECTS)


def resolve(lane: str, scope_spec: dict) -> str:
    """The lane of a new task: the given one (`now` capped), or by size — one subject → `now`, more → `queue`.

    Raises `ValueError` with a plain message when `now` is asked for more than the cap (or for a scope
    that is not a list of subjects).
    """
    refs = scope_spec.get("refs") if scope_spec.get("mode") == "list" else None
    if not lane:
        return TaskLane.NOW.value if refs is not None and len(refs) == 1 else TaskLane.QUEUE.value
    if lane == TaskLane.QUEUE.value:
        return lane
    if refs is None or len(refs) > run_now_max():
        raise ValueError(f"Run now takes at most {run_now_max()} items at a time. Add this run to the queue instead.")
    return lane


def queued_ahead(tasks: list[EnrichmentTask]) -> dict[int, int | None]:
    """`{task id: open internal tasks of its lane created before it}`; None once it runs or for an external worker."""
    internal = set(Worker.objects.filter(active=True, mode=WorkerMode.INTERNAL.value).values_list("key", flat=True))
    waiting = [t for t in tasks if t.status == TaskStatus.OPEN.value and t.type in internal]
    if not waiting:
        return {t.pk: None for t in tasks}
    rows = EnrichmentTask.objects.filter(status=TaskStatus.OPEN.value, type__in=internal).values_list(
        "lane", "created_at"
    )
    by_lane: dict[str, list] = defaultdict(list)
    for lane, created_at in rows:
        by_lane[lane].append(created_at)
    for created in by_lane.values():
        created.sort()
    ahead = {t.pk: bisect_left(by_lane[t.lane], t.created_at) for t in waiting}
    return {t.pk: ahead.get(t.pk) for t in tasks}
