# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Cost ledger (contract A4 § 1) — post factum only, from the cost each toolbox call reports.

`charge` is the only writer of `EnrichmentTask.cost_usd / cost_breakdown / cost_complete`: one row-locked
read-modify-write per call, so concurrent calls never lose an update. A call without a cost (`None`) adds
nothing and marks the task incomplete — an unknown cost never becomes 0. Spreading one call's cost over its
drafts is `kinds.base.with_cost`. No estimates, no markup.
"""

from decimal import Decimal
from typing import Any

from django.db import transaction

from django_enrichment.models import EnrichmentTask

SOURCES = frozenset({"completion", "translate", "seo", "media"})


def charge(task: Any, source: str, cost: Decimal | None) -> None:
    """Add one call's cost to its task. No task (or an unsaved stand-in) → nothing to charge."""
    if source not in SOURCES:
        raise ValueError(f"unknown cost source {source!r}")
    if getattr(task, "pk", None) is None:
        return
    with transaction.atomic():
        locked = EnrichmentTask.objects.select_for_update().get(pk=task.pk)
        _add(locked, source, cost)
        locked.save(update_fields=["cost_usd", "cost_breakdown", "cost_complete", "modified_at"])
    task.cost_usd, task.cost_breakdown, task.cost_complete = (
        locked.cost_usd,
        locked.cost_breakdown,
        locked.cost_complete,
    )


def _add(task: EnrichmentTask, source: str, cost: Decimal | None) -> None:
    if cost is None:
        task.cost_complete = False
        return
    task.cost_usd = (task.cost_usd or Decimal(0)) + cost
    task.cost_breakdown[source] = str(Decimal(task.cost_breakdown.get(source, "0")) + cost)
