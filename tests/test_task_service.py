# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import timedelta

import pytest
from django.utils import timezone

from django_enrichment.enums import ProposalStatus, TaskStatus
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import task_service


def _scope(mode: str, **extra) -> dict:
    return {"mode": mode, "module": "pim", **extra}


def _candidate(ref: str, kind: str = "attribute_value") -> dict:
    return {"subject_ref": ref, "target_kind": kind, "target_module": "pim", "target_locator": {}}


def _gap_task(*candidates, params=None):
    """Gap task whose fake-adapter candidates are seeded through scope.candidates."""
    return task_service.spawn(
        type="fix-attribute",
        scope_spec=_scope("gap", check="c", scope={"candidates": list(candidates)}),
        params=params or {},
    )


def _proposal(ref: str, status: str, *, kind: str = "attribute_value", reviewed_days_ago=None, applied_days_ago=None):
    return ContentProposal.objects.create(
        target_module="pim",
        subject_ref=ref,
        target_kind=kind,
        target_locator={"feature_idx": "description", "language": "pl"},
        proposed_value={"text": "x"},
        status=status,
        reviewed_at=timezone.now() - timedelta(days=reviewed_days_ago) if reviewed_days_ago is not None else None,
        applied_at=timezone.now() - timedelta(days=applied_days_ago) if applied_days_ago is not None else None,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["filter", "csv", "list", "gap"])
def test_spawn_accepts_every_valid_mode(mode):
    # csv carries a staged file ref (etap-11); the others don't need one.
    extra = {"file": "staged-ref.csv", "row_type": "fix-attribute"} if mode == "csv" else {}
    task = task_service.spawn(type="fix-attribute", scope_spec=_scope(mode, **extra))
    assert task.pk is not None
    assert task.status == TaskStatus.OPEN.value


@pytest.mark.django_db
def test_spawn_rejects_invalid_mode():
    with pytest.raises(ValueError, match="scope_spec.mode"):
        task_service.spawn(type="x", scope_spec={"mode": "bogus", "module": "pim"})


@pytest.mark.django_db
def test_spawn_requires_module():
    with pytest.raises(ValueError, match="scope_spec.module"):
        task_service.spawn(type="x", scope_spec={"mode": "list", "refs": ["A"]})


@pytest.mark.django_db
def test_spawn_batch_key_appends_refs_deduped_order_kept():
    first = task_service.spawn(type="x", scope_spec=_scope("list", refs=["A", "B"]), batch_key="run-1")
    second = task_service.spawn(type="x", scope_spec=_scope("list", refs=["B", "C"]), batch_key="run-1")

    assert second.pk == first.pk
    assert EnrichmentTask.objects.count() == 1
    second.refresh_from_db()
    assert second.scope_spec["refs"] == ["A", "B", "C"]


@pytest.mark.django_db
def test_spawn_batch_key_returns_existing_for_non_list_mode():
    first = task_service.spawn(type="x", scope_spec=_scope("filter", refs=["A"]), batch_key="run-1")
    second = task_service.spawn(type="x", scope_spec=_scope("filter", refs=["B"]), batch_key="run-1")

    assert second.pk == first.pk  # coalesced, but no ref merge for non-list modes
    assert EnrichmentTask.objects.count() == 1
    second.refresh_from_db()
    assert second.scope_spec["refs"] == ["A"]


@pytest.mark.django_db
def test_spawn_without_batch_key_always_creates():
    task_service.spawn(type="x", scope_spec=_scope("list", refs=["A"]))
    task_service.spawn(type="x", scope_spec=_scope("list", refs=["A"]))
    assert EnrichmentTask.objects.count() == 2


@pytest.mark.django_db
def test_list_open_filters_status_and_type():
    open_task = task_service.spawn(type="translate", scope_spec=_scope("list", refs=["A"]))
    done = task_service.spawn(type="translate", scope_spec=_scope("list", refs=["B"]))
    task_service.transition_status(done, TaskStatus.IN_PROGRESS.value)
    task_service.mark_done(done)

    result = task_service.list_open(type="translate")
    assert list(result) == [open_task]


@pytest.mark.django_db
def test_resolve_targets_filter_mode_uses_resolve_targets(fake_adapter):
    task = task_service.spawn(type="x", scope_spec=_scope("filter", refs=["SKU-1", "SKU-2"]))
    assert task_service.resolve_targets(task) == ["SKU-1", "SKU-2"]


@pytest.mark.django_db
def test_resolve_targets_gap_mode_uses_find_gaps(fake_adapter):
    task = task_service.spawn(type="x", scope_spec=_scope("gap", check="missing_feature"))
    assert task_service.resolve_targets(task) == ["gap:missing_feature"]


# ── gap-mode dedup/cooldown filter + limit budget (etap-13) ────────────────────────────────


@pytest.mark.django_db
def test_gap_filter_pending_blocks(fake_adapter):
    task = _gap_task(_candidate("S1"), _candidate("S2"))
    _proposal("S1", ProposalStatus.PENDING.value)

    refs = [c["subject_ref"] for c in task_service.resolve_targets(task)]

    assert refs == ["S2"]


@pytest.mark.django_db
def test_gap_filter_drifted_blocks(fake_adapter):
    task = _gap_task(_candidate("S1"))
    _proposal("S1", ProposalStatus.DRIFTED.value)

    assert task_service.resolve_targets(task) == []


@pytest.mark.django_db
def test_gap_filter_rejected_blocks_within_cooldown(fake_adapter):
    task = _gap_task(_candidate("S1"))
    _proposal("S1", ProposalStatus.REJECTED.value, reviewed_days_ago=2)

    assert task_service.resolve_targets(task) == []


@pytest.mark.django_db
def test_gap_filter_rejected_passes_after_cooldown(fake_adapter):
    task = _gap_task(_candidate("S1"))
    _proposal("S1", ProposalStatus.REJECTED.value, reviewed_days_ago=8)  # default cooldown 7d

    assert [c["subject_ref"] for c in task_service.resolve_targets(task)] == ["S1"]


@pytest.mark.django_db
def test_gap_filter_applied_blocks_within_cooldown(fake_adapter):
    task = _gap_task(_candidate("S1"))
    _proposal("S1", ProposalStatus.APPLIED.value, applied_days_ago=2)

    assert task_service.resolve_targets(task) == []


@pytest.mark.django_db
def test_gap_filter_applied_passes_after_cooldown(fake_adapter):
    task = _gap_task(_candidate("S1"))
    _proposal("S1", ProposalStatus.APPLIED.value, applied_days_ago=8)

    assert [c["subject_ref"] for c in task_service.resolve_targets(task)] == ["S1"]


@pytest.mark.django_db
def test_gap_filter_cooldown_days_from_task_params(fake_adapter):
    task = _gap_task(_candidate("S1"), params={"cooldown_days": 1})
    _proposal("S1", ProposalStatus.REJECTED.value, reviewed_days_ago=2)  # outside the 1-day window

    assert [c["subject_ref"] for c in task_service.resolve_targets(task)] == ["S1"]


@pytest.mark.django_db
def test_gap_filter_reverted_and_superseded_never_block(fake_adapter):
    task = _gap_task(_candidate("S1"), _candidate("S2"))
    _proposal("S1", ProposalStatus.REVERTED.value, reviewed_days_ago=0)
    _proposal("S2", ProposalStatus.SUPERSEDED.value)

    refs = [c["subject_ref"] for c in task_service.resolve_targets(task)]

    assert refs == ["S1", "S2"]


@pytest.mark.django_db
def test_gap_filter_dedup_key_includes_target_kind(fake_adapter):
    # A pending TEXT proposal must not block the PICTURE candidate of the same product.
    task = _gap_task(_candidate("S1", kind="picture"))
    _proposal("S1", ProposalStatus.PENDING.value, kind="attribute_value")

    assert [c["subject_ref"] for c in task_service.resolve_targets(task)] == ["S1"]


@pytest.mark.django_db
def test_gap_filter_string_candidates_pass_through(fake_adapter):
    task = _gap_task("just-a-string", _candidate("S1"))
    _proposal("S1", ProposalStatus.PENDING.value)

    assert task_service.resolve_targets(task) == ["just-a-string"]


@pytest.mark.django_db
def test_gap_limit_reached_reads_empty(fake_adapter):
    task = _gap_task(_candidate("S1"), params={"limit": 3})
    EnrichmentTask.objects.filter(pk=task.pk).update(counts={"proposed": 3})
    task.refresh_from_db()

    assert task_service.resolve_targets(task) == []


@pytest.mark.django_db
def test_gap_limit_trims_page_to_remaining_budget(fake_adapter):
    task = _gap_task(*[_candidate(f"S{n}") for n in range(5)], params={"limit": 4})
    EnrichmentTask.objects.filter(pk=task.pk).update(counts={"proposed": 2})
    task.refresh_from_db()

    refs = [c["subject_ref"] for c in task_service.resolve_targets(task)]

    assert refs == ["S0", "S1"]  # 4 - 2 = 2 remaining


@pytest.mark.django_db
def test_list_tasks_filters_by_batch_key():
    match = task_service.spawn(type="x", scope_spec=_scope("list", refs=["A"]), batch_key="gaprule:r1")
    task_service.spawn(type="x", scope_spec=_scope("list", refs=["B"]), batch_key="other")

    result = list(task_service.list_tasks(batch_key="gaprule:r1"))

    assert [t.pk for t in result] == [match.pk]


@pytest.mark.django_db
def test_mark_done_requires_in_progress():
    task = task_service.spawn(type="x", scope_spec=_scope("list", refs=["A"]))
    with pytest.raises(ValueError, match="invalid task transition"):
        task_service.mark_done(task)  # open -> done is not a legal edge


@pytest.mark.django_db
def test_worker_lifecycle_in_progress_then_done():
    task = task_service.spawn(type="x", scope_spec=_scope("list", refs=["A"]))

    task_service.mark_in_progress(task)
    assert task.status == TaskStatus.IN_PROGRESS.value

    done = task_service.mark_done(task)
    assert done.status == TaskStatus.DONE.value


@pytest.mark.django_db
def test_increment_count_accumulates():
    task = task_service.spawn(type="x", scope_spec=_scope("list", refs=["A"]))
    task_service.increment_count(task, "proposed")
    task_service.increment_count(task, "proposed", by=2)
    task.refresh_from_db()
    assert task.counts == {"proposed": 3}
