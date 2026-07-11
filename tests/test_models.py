# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from decimal import Decimal

import pytest

from django_enrichment.enums import PROPOSAL_STATUS_TRANSITIONS, TASK_STATUS_TRANSITIONS, ProposalStatus, TaskStatus
from django_enrichment.models import ContentProposal, EnrichmentTask, compute_locator_hash


def _make_proposal(**overrides) -> ContentProposal:
    defaults = {
        "target_module": "pim",
        "subject_ref": "SKU-1",
        "target_kind": "attribute_value",
        "target_locator": {"feature_idx": "description", "language": "pl"},
        "proposed_value": {"text": "Nowy opis"},
    }
    defaults.update(overrides)
    return ContentProposal.objects.create(**defaults)


@pytest.mark.django_db
def test_enrichment_task_defaults():
    # Arrange / Act
    task = EnrichmentTask.objects.create(type="fix-attribute")

    # Assert
    assert task.params == {}
    assert task.scope_spec == {}
    assert task.counts == {}
    assert task.batch_key == ""
    assert task.status == TaskStatus.OPEN
    assert task.requested_by is None


@pytest.mark.django_db
def test_content_proposal_defaults():
    # Arrange / Act
    proposal = _make_proposal()

    # Assert
    assert proposal.status == ProposalStatus.PENDING
    assert proposal.confidence is None
    assert proposal.current_snapshot == {}
    assert proposal.staged_file == ""
    assert proposal.task is None


@pytest.mark.django_db
def test_enrichment_task_str():
    task = EnrichmentTask.objects.create(type="translate", status=TaskStatus.IN_PROGRESS)
    assert str(task) == "EnrichmentTask(type=translate, status=in_progress)"


@pytest.mark.django_db
def test_content_proposal_str():
    proposal = _make_proposal()
    assert str(proposal) == "ContentProposal(pim:SKU-1:attribute_value [pending])"


@pytest.mark.django_db
def test_locator_hash_computed_on_save():
    locator = {"feature_idx": "description", "language": "pl"}
    proposal = _make_proposal(target_locator=locator)
    assert proposal.locator_hash == compute_locator_hash(locator)
    assert len(proposal.locator_hash) == 40


@pytest.mark.django_db
def test_locator_hash_deterministic_across_key_order():
    a = _make_proposal(target_locator={"feature_idx": "description", "language": "pl"})
    b = _make_proposal(target_locator={"language": "pl", "feature_idx": "description"})
    assert a.locator_hash == b.locator_hash


@pytest.mark.django_db
def test_locator_hash_differs_for_different_locator():
    a = _make_proposal(target_locator={"feature_idx": "description", "language": "pl"})
    b = _make_proposal(target_locator={"feature_idx": "description", "language": "en"})
    assert a.locator_hash != b.locator_hash


def test_task_transition_allowed():
    assert TaskStatus.IN_PROGRESS.value in TASK_STATUS_TRANSITIONS[TaskStatus.OPEN.value]


def test_task_transition_forbidden():
    assert TaskStatus.DONE.value not in TASK_STATUS_TRANSITIONS[TaskStatus.OPEN.value]


def test_proposal_transition_allowed():
    assert ProposalStatus.APPLIED.value in PROPOSAL_STATUS_TRANSITIONS[ProposalStatus.PENDING.value]


def test_proposal_applied_only_reverts():
    # Single-level undo (etap-09): applied has exactly one exit — reverted (terminal).
    assert PROPOSAL_STATUS_TRANSITIONS[ProposalStatus.APPLIED.value] == frozenset({ProposalStatus.REVERTED.value})
    assert PROPOSAL_STATUS_TRANSITIONS[ProposalStatus.REVERTED.value] == frozenset()


def test_drifted_to_applied_allowed():
    assert ProposalStatus.APPLIED.value in PROPOSAL_STATUS_TRANSITIONS[ProposalStatus.DRIFTED.value]


@pytest.mark.django_db
def test_same_target_proposals_share_hash_and_coexist():
    locator = {"feature_idx": "description", "language": "pl"}
    first = _make_proposal(target_locator=locator)
    second = _make_proposal(target_locator=locator)

    assert first.locator_hash == second.locator_hash
    assert first.pk != second.pk
    # No DB unique constraint — superseding is the service's job (etap-03).
    assert ContentProposal.objects.filter(locator_hash=first.locator_hash).count() == 2


@pytest.mark.django_db
def test_confidence_precision_round_trip():
    proposal = _make_proposal(confidence=Decimal("0.875"))
    proposal.refresh_from_db()
    assert proposal.confidence == Decimal("0.875")


@pytest.mark.django_db
def test_spawn_rule_defaults_and_str():
    from django_enrichment.models import SpawnRule

    rule = SpawnRule.objects.create(key="r1", module="pim", check_key="pl-description", task_type="fix-attribute")

    assert rule.params == {}
    assert rule.scope == {}
    assert rule.task_params == {}
    assert rule.limit is None
    assert rule.cooldown_days is None
    assert rule.auto is False
    assert rule.active is True
    assert str(rule) == "SpawnRule(key=r1, module=pim, check=pl-description)"


@pytest.mark.django_db
def test_spawn_rule_key_is_unique():
    from django.db import IntegrityError

    from django_enrichment.models import SpawnRule

    SpawnRule.objects.create(key="dup", module="pim", check_key="c", task_type="t")
    with pytest.raises(IntegrityError):
        SpawnRule.objects.create(key="dup", module="pim", check_key="c2", task_type="t2")
