# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery beat task for retention cleanup (etap-10). Thin — all logic lives in `cleanup_service`.

The host service schedules `enrichment.prune` daily (see `docs/scale-and-undo.md` § Retention) on the
`enrichment` queue and runs a worker that consumes it. Out-of-band and idempotent: a re-run finds
nothing and never double-deletes (staged delete no-ops on a missing ref, the adapter GC is
reference-guarded). The window comes from `ENRICHMENT_RETENTION_DAYS` — the beat passes no override.
"""

from typing import Any

from celery import shared_task


@shared_task(name="enrichment.prune")
def prune_task() -> dict[str, Any]:
    """Daily retention pass — prune terminal proposals + done tasks older than the retention window."""
    from django_enrichment.services import cleanup_service

    return cleanup_service.prune()
