# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""`enrichment.comfy_spawn` — the next batch of main pictures for every active `comfy` worker.

The host schedules it, and its schedule is the time window: `crontab(minute="*/15", hour="22-23,0-5")` keeps a
shared GPU busy only at night. The units it spawns run on the bulk lane (`ENRICHMENT_BULK_QUEUE`, concurrency 1).
"""

from celery import shared_task


@shared_task(name="enrichment.comfy_spawn")
def comfy_spawn_task() -> dict[str, list[str]]:
    from django_enrichment.services import comfy_spawn_service

    return comfy_spawn_service.spawn_all()
