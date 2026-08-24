# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from decimal import Decimal

import pytest

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal
from django_enrichment.services import proposal_service


def _boom(proposal) -> None:
    raise RuntimeError("adapter exploded")


def _intake(fake_adapter, *, subject_ref="SKU-1", proposed=None, **extra) -> ContentProposal:
    return proposal_service.intake(
        target_module="pim",
        subject_ref=subject_ref,
        target_kind="attribute_value",
        target_locator={"feature_idx": "description", "language": "pl"},
        proposed_value=proposed or {"text": "Nowy opis"},
        **extra,
    )


@pytest.mark.django_db
def test_intake_snapshots_live_value_not_worker_before(fake_adapter):
    fake_adapter.set_current(
        "SKU-1", "attribute_value", {"feature_idx": "description", "language": "pl"}, {"text": "old"}
    )

    proposal = _intake(fake_adapter)

    assert proposal.status == ProposalStatus.PENDING.value
    assert proposal.current_snapshot == {"text": "old"}  # from read_current, authoritative


@pytest.mark.django_db
def test_intake_is_idempotent_on_external_ref(fake_adapter):
    first = _intake(fake_adapter, external_ref="n8n-42")
    second = _intake(fake_adapter, external_ref="n8n-42", proposed={"text": "different"})

    assert second.pk == first.pk
    assert ContentProposal.objects.count() == 1


@pytest.mark.django_db
def test_intake_supersedes_older_pending_on_same_target(fake_adapter):
    first = _intake(fake_adapter)
    second = _intake(fake_adapter, proposed={"text": "newer"})

    first.refresh_from_db()
    assert first.status == ProposalStatus.SUPERSEDED.value
    assert second.status == ProposalStatus.PENDING.value


@pytest.mark.django_db
def test_intake_increments_task_count(fake_adapter):
    from django_enrichment.services import task_service

    task = task_service.spawn(type="x", scope_spec={"mode": "list", "module": "pim", "refs": ["SKU-1"]})
    _intake(fake_adapter, task=task)
    task.refresh_from_db()
    assert task.counts == {"proposed": 1}


def test_transition_forbidden_from_terminal():
    proposal = ContentProposal(status=ProposalStatus.APPLIED.value)
    with pytest.raises(ValueError, match="invalid proposal transition"):
        proposal_service.transition_status(proposal, ProposalStatus.REJECTED.value)


@pytest.mark.django_db
def test_accept_writes_through_adapter_and_marks_applied(fake_adapter, admin_user):
    proposal = _intake(fake_adapter, proposed={"text": "fresh"})

    result = proposal_service.accept(proposal, user=admin_user)

    assert result.status == ProposalStatus.APPLIED.value
    assert result.applied_at is not None
    assert result.reviewed_by == admin_user
    assert fake_adapter.read_current(
        subject_ref="SKU-1",
        target_kind="attribute_value",
        target_locator={"feature_idx": "description", "language": "pl"},
    ) == {"text": "fresh"}


@pytest.mark.django_db
def test_accept_detects_drift_and_does_not_write(fake_adapter, admin_user):
    proposal = _intake(fake_adapter, proposed={"text": "fresh"})  # snapshot {}
    fake_adapter.set_current(
        "SKU-1", "attribute_value", {"feature_idx": "description", "language": "pl"}, {"text": "changed live"}
    )

    result = proposal_service.accept(proposal, user=admin_user)

    assert result.status == ProposalStatus.DRIFTED.value
    assert result.current_snapshot == {"text": "changed live"}  # refreshed for the re-confirm view
    # proposed_value was NOT written — the live value is untouched.
    assert fake_adapter.read_current(
        subject_ref="SKU-1",
        target_kind="attribute_value",
        target_locator={"feature_idx": "description", "language": "pl"},
    ) == {"text": "changed live"}


@pytest.mark.django_db
def test_drifted_accept_reconfirms_and_applies(fake_adapter, admin_user):
    proposal = _intake(fake_adapter, proposed={"text": "fresh"})
    fake_adapter.set_current(
        "SKU-1", "attribute_value", {"feature_idx": "description", "language": "pl"}, {"text": "changed live"}
    )
    proposal_service.accept(proposal, user=admin_user)  # -> drifted

    result = proposal_service.accept(proposal, user=admin_user)  # operator re-confirms

    assert result.status == ProposalStatus.APPLIED.value
    assert fake_adapter.read_current(
        subject_ref="SKU-1",
        target_kind="attribute_value",
        target_locator={"feature_idx": "description", "language": "pl"},
    ) == {"text": "fresh"}


@pytest.mark.django_db
def test_reject_sets_reason_and_audit(fake_adapter, admin_user):
    proposal = _intake(fake_adapter)

    result = proposal_service.reject(proposal, "low quality", user=admin_user)

    assert result.status == ProposalStatus.REJECTED.value
    assert result.reject_reason == "low quality"
    assert result.reviewed_by == admin_user


@pytest.mark.django_db
def test_reject_notifies_the_source_module(fake_adapter, admin_user):
    proposal = _intake(fake_adapter)

    proposal_service.reject(proposal, "not the same product", user=admin_user)

    assert fake_adapter.rejected() == [proposal.pk]


@pytest.mark.django_db
def test_reject_survives_a_broken_adapter_hook(fake_adapter, admin_user, monkeypatch):
    """The rejection is already committed — a failing hook must not turn it into a 500."""
    proposal = _intake(fake_adapter)
    monkeypatch.setattr(fake_adapter, "on_reject", _boom)

    result = proposal_service.reject(proposal, "low quality", user=admin_user)

    assert result.status == ProposalStatus.REJECTED.value


@pytest.mark.django_db
def test_apply_many_buckets_applied_drifted_failed(fake_adapter, admin_user):
    p_ok = _intake(fake_adapter, subject_ref="SKU-OK")
    p_drift = _intake(fake_adapter, subject_ref="SKU-DRIFT")
    fake_adapter.set_current(
        "SKU-DRIFT", "attribute_value", {"feature_idx": "description", "language": "pl"}, {"text": "moved"}
    )
    p_failed = _intake(fake_adapter, subject_ref="SKU-FAIL")
    proposal_service.reject(p_failed, "nope", user=admin_user)  # terminal -> apply will fail

    result = proposal_service.apply_many([p_ok.pk, p_drift.pk, p_failed.pk], user_id=admin_user.pk)

    # Per-bucket set comparison: stays correct if a bucket ever grows past one element
    # (queryset order is not guaranteed).
    assert set(result["applied"]) == {p_ok.pk}
    assert set(result["drifted"]) == {p_drift.pk}
    assert set(result["failed"]) == {p_failed.pk}


@pytest.mark.django_db
def test_bulk_accept_below_threshold_applies_sync_and_threads_user(fake_adapter, admin_user):
    _intake(fake_adapter, subject_ref="SKU-A")
    _intake(fake_adapter, subject_ref="SKU-B")

    # 2 proposals <= default threshold 50 -> sync, counts inline (no Celery hop).
    result = proposal_service.bulk_accept({"module": "pim"}, user=admin_user)

    assert result["mode"] == "sync"
    assert result["enqueued"] is None
    assert len(result["applied"]) == 2
    applied = ContentProposal.objects.filter(status=ProposalStatus.APPLIED.value)
    assert applied.count() == 2
    assert all(p.reviewed_by == admin_user for p in applied)


@pytest.mark.django_db
def test_bulk_accept_above_threshold_dispatches_canvas(fake_adapter, admin_user, settings, monkeypatch):
    settings.ENRICHMENT_ASYNC_APPLY_THRESHOLD = 1  # force the async branch with 2 proposals
    _intake(fake_adapter, subject_ref="SKU-A")
    _intake(fake_adapter, subject_ref="SKU-B")
    calls = {}
    from django_enrichment.tasks import apply_tasks

    monkeypatch.setattr(apply_tasks, "dispatch_canvas", lambda ids, **kw: calls.update(ids=list(ids), **kw))

    result = proposal_service.bulk_accept({"module": "pim"}, user=admin_user)

    assert result["mode"] == "async"
    assert result["enqueued"] == 2
    assert result["applied"] is None
    assert len(calls["ids"]) == 2 and calls["op"] == "apply"  # routed to the canvas, not applied in-request
    assert ContentProposal.objects.filter(status=ProposalStatus.PENDING.value).count() == 2


@pytest.mark.django_db
def test_bulk_reject_is_synchronous_update(fake_adapter, admin_user):
    _intake(fake_adapter, subject_ref="SKU-A")
    _intake(fake_adapter, subject_ref="SKU-B")

    result = proposal_service.bulk_reject({"module": "pim"}, "batch reject", user=admin_user)

    assert result == {"rejected": 2}
    assert ContentProposal.objects.filter(status=ProposalStatus.REJECTED.value).count() == 2
    assert len(fake_adapter.rejected()) == 2  # every rejected row reaches the source module


@pytest.mark.django_db
def test_bulk_reject_skips_the_hook_for_an_adapter_without_one(fake_adapter, admin_user, monkeypatch):
    """Most adapters (PIM) have no `on_reject` — the single-UPDATE path must stay untouched."""
    _intake(fake_adapter, subject_ref="SKU-A")
    monkeypatch.delattr(fake_adapter, "on_reject")

    result = proposal_service.bulk_reject({"module": "pim"}, "batch reject", user=admin_user)

    assert result == {"rejected": 1}


@pytest.mark.django_db
def test_bulk_undo_below_threshold_reverts_applied_sync(fake_adapter, admin_user):
    # Seed two applied proposals (intake -> accept) on different targets.
    for sku in ("SKU-A", "SKU-B"):
        proposal_service.accept(_intake(fake_adapter, subject_ref=sku), user=admin_user)
    assert ContentProposal.objects.filter(status=ProposalStatus.APPLIED.value).count() == 2

    result = proposal_service.bulk_undo({"module": "pim"}, user=admin_user)

    assert result["mode"] == "sync"
    assert result["enqueued"] is None
    assert len(result["reverted"]) == 2
    assert ContentProposal.objects.filter(status=ProposalStatus.REVERTED.value).count() == 2


@pytest.mark.django_db
def test_bulk_undo_blocks_item_drifted_since_apply(fake_adapter, admin_user):
    locator = {"feature_idx": "description", "language": "pl"}
    p = proposal_service.accept(_intake(fake_adapter, subject_ref="SKU-A"), user=admin_user)
    # Someone edits the target after our apply -> live != applied_snapshot -> undo must refuse.
    fake_adapter.set_current("SKU-A", "attribute_value", locator, {"text": "edited after apply"})

    result = proposal_service.bulk_undo({"module": "pim"}, user=admin_user)

    assert result["reverted"] == []
    assert result["blocked"] == [p.pk]
    p.refresh_from_db()
    assert p.status == ProposalStatus.APPLIED.value  # stays applied, not clobbered


@pytest.mark.django_db
def test_list_for_review_applies_filters(fake_adapter):
    keep = _intake(fake_adapter, subject_ref="SKU-KEEP", confidence=Decimal("0.90"))
    _intake(fake_adapter, subject_ref="SKU-LOWCONF", confidence=Decimal("0.10"))
    proposal_service.reject(_intake(fake_adapter, subject_ref="SKU-REJECTED"), "x")

    result = proposal_service.list_for_review(module="pim", confidence_min=Decimal("0.50"))

    assert list(result) == [keep]  # default status=pending excludes the rejected one; low-conf filtered out
