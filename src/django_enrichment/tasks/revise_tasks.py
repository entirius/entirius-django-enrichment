# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery entry of the batch correction. Thin — the logic lives in `revise_batch_service`."""

from celery import shared_task


@shared_task(name="enrichment.revise_task")
def revise_task(task_id: int, correction: dict, user_id: int | None = None) -> None:
    from django_enrichment.services import revise_batch_service

    revise_batch_service.revise_pending(task_id, correction, user_id=user_id)
