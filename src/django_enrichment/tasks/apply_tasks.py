# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery canvas for mass apply / undo. Thin — all per-row logic lives in `proposal_service`.

Mass apply (and undo) of 10k+ proposals can't run as one task: materialising and applying the whole set
in a single worker risks OOM (research R3). `dispatch_canvas` fans the id list out into fixed-size
chunks (`chunks`), each chunk applied/reverted independently, with a `chord` callback aggregating the
per-chunk result dicts once the group finishes — "counts via chords" (decision PO etap-09). Each chunk's
`proposal_service` core uses `.iterator()`, so per-chunk memory is bounded by `ENRICHMENT_APPLY_BATCH_SIZE`.

Heavy apply runs on a dedicated queue (`ENRICHMENT_CELERY_QUEUE`, default `enrichment`) so it never
starves the default queue — the host service registers the queue + a worker consuming it.

The bus only reaches here above the sync threshold; small batches apply in-request (`proposal_service`).
"""

import logging
from typing import Any

from celery import chord, shared_task
from django.conf import settings

from django_enrichment import settings as enrichment_settings

_logger = logging.getLogger("process")


def _queue() -> str:
    return getattr(settings, "ENRICHMENT_CELERY_QUEUE", enrichment_settings.ENRICHMENT_CELERY_QUEUE)


def _batch_size() -> int:
    return getattr(settings, "ENRICHMENT_APPLY_BATCH_SIZE", enrichment_settings.ENRICHMENT_APPLY_BATCH_SIZE)


@shared_task(name="enrichment.apply_chunk")
def apply_chunk_task(proposal_ids: list[int], user_id: int | None = None, op: str = "apply") -> dict[str, Any]:
    """Apply or revert one chunk of proposals. `op` selects the `proposal_service` core."""
    from django_enrichment.services import proposal_service

    core = proposal_service.revert_many if op == "revert" else proposal_service.apply_many
    return core(proposal_ids, user_id=user_id)


@shared_task(name="enrichment.finalize_batch")
def finalize_batch_task(chunk_results: list[dict[str, Any]], op: str = "apply") -> dict[str, Any]:
    """Chord callback — sum the per-chunk result lists into batch totals and log them.

    Aggregates the batch (not per-task `counts`): a filter-driven bulk spans many tasks, so summing here
    is the meaningful unit. Per-task progress counters stay query-derivable (out of scope, etap-09).
    """
    keys = ("reverted", "blocked", "failed") if op == "revert" else ("applied", "drifted", "failed")
    totals = {key: sum(len(r.get(key, [])) for r in chunk_results) for key in keys}
    _logger.info("enrichment %s batch finalized: %s", op, totals)
    return {"op": op, **totals}


def dispatch_canvas(proposal_ids: list[int], *, user_id: int | None = None, op: str = "apply") -> None:
    """Fan `proposal_ids` into `batch_size` chunks on the enrichment queue, chord-aggregated on finish.

    Scale ceiling (10k target is comfortably inside it): the whole chord header is built and published
    in one broker message, so at ~1M ids / 500 = 2000 signatures the message approaches the AMQP cap.
    Past that, switch to keyset-paginated lazy dispatch (publish chunks as they're sliced) and drop the
    chord for a separate counter — out of scope here.
    """
    batch = _batch_size()
    queue = _queue()
    header = [
        apply_chunk_task.s(proposal_ids[i : i + batch], user_id, op).set(queue=queue)
        for i in range(0, len(proposal_ids), batch)
    ]
    chord(header)(finalize_batch_task.s(op).set(queue=queue))


@shared_task(name="enrichment.apply_proposals")
def apply_proposals_task(proposal_ids: list[int], user_id: int | None = None) -> dict[str, Any]:
    """Back-compat single-task apply (the etap-05 entry point). Kept so a host that registered this task
    name keeps working; new dispatch goes through `dispatch_canvas`."""
    from django_enrichment.services import proposal_service

    return proposal_service.apply_many(proposal_ids, user_id=user_id)
