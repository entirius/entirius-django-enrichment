# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Test helper: what Celery does in production — plan a task into units, drain them with the internal consumer."""

from django_enrichment.enums import WorkerMode
from django_enrichment.models import EnrichmentTask, Worker
from django_enrichment.services import consumer_service, unit_planner


def run_queue(task: EnrichmentTask) -> EnrichmentTask:
    unit_planner.plan_task(task.pk)
    worker = Worker.objects.get(key=task.type, mode=WorkerMode.INTERNAL.value)
    while consumer_service.consume(worker.key, task.lane):
        pass
    task.refresh_from_db()
    return task
