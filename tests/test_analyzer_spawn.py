# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""API-analyzer spawn (etap-11) — `batch_key` find-or-append coalescing.

The n8n analyzer loop calls spawn repeatedly with a per-run `batch_key`. Instead of one task per
product, a matching open task absorbs the new refs (one rolling task per run). The mechanism lives
in `task_service.spawn` (etap-03); this suite is the named etap-11 contract artifact.
"""

import pytest

from django_enrichment.enums import TaskStatus
from django_enrichment.models import EnrichmentTask
from django_enrichment.services import task_service


def _scope(refs, mode="list"):
    return {"mode": mode, "module": "pim", "refs": list(refs)}


@pytest.mark.django_db
def test_second_call_appends_refs_into_one_rolling_task():
    first = task_service.spawn(type="describe", scope_spec=_scope(["A", "B"]), batch_key="run-X")
    second = task_service.spawn(type="describe", scope_spec=_scope(["C"]), batch_key="run-X")

    assert second.pk == first.pk
    assert EnrichmentTask.objects.count() == 1
    second.refresh_from_db()
    assert second.scope_spec["refs"] == ["A", "B", "C"]


@pytest.mark.django_db
def test_append_dedupes_and_keeps_order():
    task_service.spawn(type="describe", scope_spec=_scope(["A", "B"]), batch_key="run-X")
    task_service.spawn(type="describe", scope_spec=_scope(["B", "C", "A"]), batch_key="run-X")
    task_service.spawn(type="describe", scope_spec=_scope(["D"]), batch_key="run-X")

    task = EnrichmentTask.objects.get(batch_key="run-X")
    assert task.scope_spec["refs"] == ["A", "B", "C", "D"]


@pytest.mark.django_db
def test_distinct_batch_keys_create_distinct_tasks():
    a = task_service.spawn(type="describe", scope_spec=_scope(["A"]), batch_key="run-X")
    b = task_service.spawn(type="describe", scope_spec=_scope(["B"]), batch_key="run-Y")

    assert a.pk != b.pk
    assert EnrichmentTask.objects.count() == 2


@pytest.mark.django_db
def test_no_batch_key_never_coalesces():
    task_service.spawn(type="describe", scope_spec=_scope(["A"]))
    task_service.spawn(type="describe", scope_spec=_scope(["A"]))
    assert EnrichmentTask.objects.count() == 2


@pytest.mark.django_db
def test_done_task_does_not_absorb_new_refs():
    """Coalescing targets OPEN tasks only — a finished run starts a fresh rolling task."""
    done = task_service.spawn(type="describe", scope_spec=_scope(["A"]), batch_key="run-X")
    done.status = TaskStatus.DONE.value
    done.save(update_fields=["status"])

    fresh = task_service.spawn(type="describe", scope_spec=_scope(["B"]), batch_key="run-X")

    assert fresh.pk != done.pk
    assert EnrichmentTask.objects.count() == 2
