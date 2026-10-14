# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery entry of the internal runner. Thin — the logic lives in `runner_service`."""

from celery import shared_task


@shared_task(name="enrichment.run_task")
def run_task(task_id: int) -> None:
    from django_enrichment.services import runner_service

    runner_service.run_task(task_id)
