# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Mass apply (etap-09) — Celery canvas chunking, iterator() memory bound, idempotency, drift isolation."""

import pytest
from django.db.models.query import QuerySet

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal
from django_enrichment.services import proposal_service
from django_enrichment.tasks import apply_tasks

_LOCATOR = {"feature_idx": "description", "language": "pl"}


def _seed(fake_adapter, n, *, prefix="SKU"):
    return [
        proposal_service.intake(
            target_module="pim",
            subject_ref=f"{prefix}-{i}",
            target_kind="attribute_value",
            target_locator=_LOCATOR,
            proposed_value={"text": f"desc {i}"},
        )
        for i in range(n)
    ]


@pytest.mark.django_db
def test_dispatch_canvas_chunks_by_batch_size_on_dedicated_queue(settings, monkeypatch):
    settings.ENRICHMENT_APPLY_BATCH_SIZE = 500
    settings.ENRICHMENT_CELERY_QUEUE = "enrichment"
    captured = {}

    def fake_chord(header):
        captured["header"] = list(header)
        return lambda callback: captured.update(callback=callback)

    monkeypatch.setattr(apply_tasks, "chord", fake_chord)

    apply_tasks.dispatch_canvas(list(range(1200)), user_id=7, op="apply")

    # 1200 ids / 500 -> 3 chunks (500, 500, 200), each on the enrichment queue, op threaded through.
    header = captured["header"]
    assert len(header) == 3
    assert all(sig.options.get("queue") == "enrichment" for sig in header)
    assert header[0].args[2] == "apply" and header[0].args[1] == 7
    assert len(header[0].args[0]) == 500 and len(header[2].args[0]) == 200
    assert captured["callback"].options.get("queue") == "enrichment"


@pytest.mark.django_db
def test_apply_many_uses_iterator_not_full_materialization(fake_adapter, monkeypatch):
    seeded = _seed(fake_adapter, 5)
    calls = {"n": 0}
    orig = QuerySet.iterator

    def spy(self, *a, **k):
        calls["n"] += 1
        return orig(self, *a, **k)

    monkeypatch.setattr(QuerySet, "iterator", spy)

    proposal_service.apply_many([p.pk for p in seeded])

    assert calls["n"] >= 1  # streamed via iterator(), never list(qs) over the whole chunk (OOM, R3)


@pytest.mark.django_db
def test_apply_many_is_idempotent_on_rerun(fake_adapter):
    seeded = _seed(fake_adapter, 3)
    ids = [p.pk for p in seeded]

    first = proposal_service.apply_many(ids)
    second = proposal_service.apply_many(ids)

    assert len(first["applied"]) == 3
    # Re-run: every proposal is already applied -> the status guard rejects it -> failed bucket, no double-write.
    assert second["applied"] == []
    assert set(second["failed"]) == set(ids)
    assert ContentProposal.objects.filter(status=ProposalStatus.APPLIED.value).count() == 3


@pytest.mark.django_db
def test_drift_in_batch_isolates_one_item(fake_adapter):
    ok, drift = _seed(fake_adapter, 2)
    # Make `drift`'s live value diverge from its intake snapshot -> apply marks it drifted, no write.
    fake_adapter.set_current(drift.subject_ref, "attribute_value", _LOCATOR, {"text": "changed live"})

    result = proposal_service.apply_many([ok.pk, drift.pk])

    assert result["applied"] == [ok.pk]
    assert result["drifted"] == [drift.pk]  # isolated — the batch was not aborted
    assert result["failed"] == []


@pytest.mark.django_db
def test_finalize_batch_aggregates_chunk_totals():
    chunks = [{"applied": [1, 2], "drifted": [3], "failed": []}, {"applied": [4], "drifted": [], "failed": [5]}]
    totals = apply_tasks.finalize_batch_task(chunks, op="apply")
    assert totals == {"op": "apply", "applied": 3, "drifted": 1, "failed": 1}


@pytest.mark.django_db
def test_canvas_smoke_applies_a_few_hundred(fake_adapter, settings):
    settings.ENRICHMENT_APPLY_BATCH_SIZE = 50  # several chunks over the seeded set
    seeded = _seed(fake_adapter, 120)

    apply_tasks.dispatch_canvas([p.pk for p in seeded], user_id=None, op="apply")

    # Celery eager (conftest) runs the chunk tasks inline; all proposals end applied.
    assert ContentProposal.objects.filter(status=ProposalStatus.APPLIED.value).count() == 120
