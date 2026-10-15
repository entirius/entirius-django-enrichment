# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery entries of the work queue. Thin — the logic lives in the services."""

from celery import shared_task


@shared_task(name="enrichment.plan_task")
def plan_task(task_id: int) -> int:
    from django_enrichment.services import unit_planner

    return unit_planner.plan_task(task_id)


@shared_task(name="enrichment.run_task")
def run_task(task_id: int) -> int:
    """Shim for messages queued before the work queue existed: plan the task, the consumer follows."""
    return plan_task(task_id)


@shared_task(name="enrichment.consume")
def consume(worker_key: str, lane: str) -> int:
    """Handle a few units of (worker, lane); re-queue itself while work remains."""
    from django_enrichment.services import consumer_service, queue_dispatch

    handled = consumer_service.consume(worker_key, lane)
    if handled and consumer_service.has_more(worker_key, lane):
        queue_dispatch.kick_consumer(worker_key, lane)
    return handled


@shared_task(name="enrichment.reap")
def reap() -> dict[str, int]:
    from django_enrichment.services import queue_reaper

    return queue_reaper.reap()
