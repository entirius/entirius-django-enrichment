# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Single-level undo (etap-09) — drift-aware revert through the adapter."""

import pytest

from django_enrichment.enums import ProposalStatus
from django_enrichment.services import proposal_service, undo_service

_LOCATOR = {"feature_idx": "description", "language": "pl"}


def _intake(fake_adapter, *, subject_ref="SKU-1", proposed=None):
    return proposal_service.intake(
        target_module="pim",
        subject_ref=subject_ref,
        target_kind="attribute_value",
        target_locator=_LOCATOR,
        proposed_value=proposed or {"text": "Nowy opis"},
    )


@pytest.mark.django_db
def test_revert_restores_snapshot_and_marks_reverted(fake_adapter, admin_user):
    fake_adapter.set_current("SKU-1", "attribute_value", _LOCATOR, {"text": "old"})
    proposal = proposal_service.accept(_intake(fake_adapter), user=admin_user)
    assert proposal.status == ProposalStatus.APPLIED.value
    # apply wrote the proposed value through the adapter
    assert fake_adapter.read_current(subject_ref="SKU-1", target_kind="attribute_value", target_locator=_LOCATOR) == {
        "text": "Nowy opis"
    }

    reverted = undo_service.revert(proposal, user=admin_user)

    assert reverted.status == ProposalStatus.REVERTED.value
    assert reverted.reviewed_by == admin_user
    # adapter.revert restored the pre-apply snapshot
    assert fake_adapter.read_current(subject_ref="SKU-1", target_kind="attribute_value", target_locator=_LOCATOR) == {
        "text": "old"
    }


@pytest.mark.django_db
def test_revert_blocks_when_target_changed_since_apply(fake_adapter, admin_user):
    proposal = proposal_service.accept(_intake(fake_adapter), user=admin_user)
    # A post-apply edit by someone else: live now diverges from applied_snapshot.
    fake_adapter.set_current("SKU-1", "attribute_value", _LOCATOR, {"text": "edited after apply"})

    with pytest.raises(undo_service.UndoBlockedError):
        undo_service.revert(proposal, user=admin_user)

    proposal.refresh_from_db()
    assert proposal.status == ProposalStatus.APPLIED.value  # untouched
    # the other person's edit survives — undo did not clobber it
    assert fake_adapter.read_current(subject_ref="SKU-1", target_kind="attribute_value", target_locator=_LOCATOR) == {
        "text": "edited after apply"
    }


@pytest.mark.django_db
def test_revert_rejects_non_applied_proposal(fake_adapter, admin_user):
    pending = _intake(fake_adapter)  # still pending

    with pytest.raises(ValueError, match="only 'applied'"):
        undo_service.revert(pending, user=admin_user)


@pytest.mark.django_db
def test_applied_snapshot_captured_on_apply(fake_adapter, admin_user):
    proposal = proposal_service.accept(_intake(fake_adapter, proposed={"text": "v2"}), user=admin_user)
    proposal.refresh_from_db()
    # applied_snapshot is the post-write live value (the undo drift anchor), generic across kinds.
    assert proposal.applied_snapshot == {"text": "v2"}
