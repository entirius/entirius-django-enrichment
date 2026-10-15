# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery hand-off of the queue: plan a task into units, run a consumer for one (worker, lane).

Always after commit and always on the lane's queue, so an interactive click never waits behind bulk work.
"""

from django.db import transaction

from django_enrichment.models import EnrichmentTask
from django_enrichment.services import lane_service


def kick_plan(task: EnrichmentTask) -> None:
    from django_enrichment.tasks.worker_runner import plan_task

    queue = lane_service.celery_queue(task.lane)
    transaction.on_commit(lambda: plan_task.apply_async(args=[task.pk], queue=queue))


def kick_consumer(worker_key: str, lane: str) -> None:
    from django_enrichment.tasks.worker_runner import consume

    queue = lane_service.celery_queue(lane)
    transaction.on_commit(lambda: consume.apply_async(args=[worker_key, lane], queue=queue))
