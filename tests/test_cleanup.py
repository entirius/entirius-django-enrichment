# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retention cleanup (etap-10) — prune per status by age, drop staged files, GC anchors, keep pending.

Uses the in-memory `fake_adapter` (its `release_undo_anchor` is a spy) so the bus flow is exercised
without django-pim. Terminal records are backdated via `.update()` to bypass `auto_now` on `modified_at`.
"""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from django_enrichment.enums import ProposalStatus, TaskStatus
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import cleanup_service, staging_store

_LOCATOR = {"feature_idx": "description", "language": "pl"}


def _make(status, *, ref="SKU-1", kind="attribute_value", staged="", task=None, snapshot=None):
    return ContentProposal.objects.create(
        target_module="pim",
        subject_ref=ref,
        target_kind=kind,
        target_locator=_LOCATOR if kind == "attribute_value" else {"channel": "eu"},
        proposed_value={"text": "x"} if kind == "attribute_value" else {"op": "replace_main"},
        current_snapshot=snapshot or {},
        status=status,
        staged_file=staged,
        task=task,
        applied_at=timezone.now() if status == ProposalStatus.APPLIED.value else None,
    )


def _backdate(*proposals, days):
    old = timezone.now() - timedelta(days=days)
    ContentProposal.objects.filter(pk__in=[p.pk for p in proposals]).update(applied_at=old, modified_at=old)


@pytest.mark.django_db
def test_prune_deletes_aged_applied_keeps_fresh(fake_adapter):
    aged = _make(ProposalStatus.APPLIED.value, ref="OLD")
    fresh = _make(ProposalStatus.APPLIED.value, ref="NEW")
    _backdate(aged, days=40)  # fresh keeps applied_at = now

    result = cleanup_service.prune(days=30)

    assert result["deleted_proposals"] == 1
    assert not ContentProposal.objects.filter(pk=aged.pk).exists()
    assert ContentProposal.objects.filter(pk=fresh.pk).exists()


@pytest.mark.django_db
def test_prune_deletes_aged_rejected_and_drops_staged_file(fake_adapter, settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    ref = staging_store.save(SimpleUploadedFile("x.png", b"binary-bytes", content_type="image/png"))
    assert staging_store.exists(ref)
    rejected = _make(ProposalStatus.REJECTED.value, kind="picture", staged=ref)
    _backdate(rejected, days=40)

    result = cleanup_service.prune(days=30)

    assert result["deleted_proposals"] == 1
    assert result["staged_deleted"] == 1
    assert not staging_store.exists(ref)  # physically gone, not just the DB row
    assert not ContentProposal.objects.filter(pk=rejected.pk).exists()


@pytest.mark.django_db
def test_prune_deletes_all_other_terminal_statuses(fake_adapter):
    statuses = [ProposalStatus.SUPERSEDED.value, ProposalStatus.REVERTED.value, ProposalStatus.DRIFTED.value]
    made = [_make(s, ref=f"SKU-{s}") for s in statuses]
    _backdate(*made, days=40)

    result = cleanup_service.prune(days=30)

    assert result["deleted_proposals"] == 3
    assert ContentProposal.objects.count() == 0


@pytest.mark.django_db
def test_prune_never_deletes_pending(fake_adapter):
    pending = _make(ProposalStatus.PENDING.value)
    _backdate(pending, days=400)  # ancient, but pending is live review work — untouchable

    result = cleanup_service.prune(days=30)

    assert result["deleted_proposals"] == 0
    assert ContentProposal.objects.filter(pk=pending.pk).exists()


@pytest.mark.django_db
def test_prune_deletes_done_task_and_nulls_surviving_proposal_link(fake_adapter):
    task = EnrichmentTask.objects.create(type="translate", status=TaskStatus.DONE.value)
    EnrichmentTask.objects.filter(pk=task.pk).update(modified_at=timezone.now() - timedelta(days=40))
    survivor = _make(ProposalStatus.PENDING.value, task=task)  # pending → survives the prune
    fresh_task = EnrichmentTask.objects.create(type="translate", status=TaskStatus.DONE.value)

    result = cleanup_service.prune(days=30)

    assert result["deleted_tasks"] == 1
    assert not EnrichmentTask.objects.filter(pk=task.pk).exists()
    assert EnrichmentTask.objects.filter(pk=fresh_task.pk).exists()  # fresh done task kept
    survivor.refresh_from_db()
    assert survivor.task_id is None  # FK SET_NULL — proposal survives, link nulled


@pytest.mark.django_db
def test_prune_invokes_release_undo_anchor_for_picture_only(fake_adapter):
    pic = _make(ProposalStatus.APPLIED.value, ref="PIC", kind="picture", snapshot={"sha1": "abc"})
    txt = _make(ProposalStatus.APPLIED.value, ref="TXT")
    _backdate(pic, txt, days=40)

    cleanup_service.prune(days=30)

    released = fake_adapter.released()
    assert pic.pk in released
    assert txt.pk not in released  # text has no external anchor — hook not called


@pytest.mark.django_db
def test_prune_days_zero_deletes_everything_qualifying(fake_adapter):
    _make(ProposalStatus.APPLIED.value, ref="A")
    _make(ProposalStatus.REJECTED.value, ref="B")
    pending = _make(ProposalStatus.PENDING.value, ref="C")

    result = cleanup_service.prune(days=0)

    assert result["deleted_proposals"] == 2  # applied + rejected (just created, < now)
    assert ContentProposal.objects.filter(pk=pending.pk).exists()  # pending still spared


@pytest.mark.django_db
def test_prune_is_idempotent_on_rerun(fake_adapter):
    aged = _make(ProposalStatus.REJECTED.value)
    _backdate(aged, days=40)

    first = cleanup_service.prune(days=30)
    second = cleanup_service.prune(days=30)

    assert first["deleted_proposals"] == 1
    assert second["deleted_proposals"] == 0  # nothing left, no error


@pytest.mark.django_db
def test_prune_negative_days_raises(fake_adapter):
    with pytest.raises(ValueError, match="days must be >= 0"):
        cleanup_service.prune(days=-1)


@pytest.mark.django_db
def test_prune_uses_retention_setting_when_days_omitted(fake_adapter, settings):
    settings.ENRICHMENT_RETENTION_DAYS = 10
    aged = _make(ProposalStatus.REJECTED.value, ref="AGED")
    recent = _make(ProposalStatus.REJECTED.value, ref="RECENT")
    _backdate(aged, days=15)
    _backdate(recent, days=5)

    result = cleanup_service.prune()  # no explicit days → setting (10)

    assert result["retention_days"] == 10
    assert not ContentProposal.objects.filter(pk=aged.pk).exists()
    assert ContentProposal.objects.filter(pk=recent.pk).exists()


# ── CSV staged-file cleanup (etap-11) ──────────────────────────────────────────────────────────


def _csv_task(ref, *, status=TaskStatus.DONE.value):
    return EnrichmentTask.objects.create(
        type="fix-attribute",
        status=status,
        scope_spec={"mode": "csv", "module": "pim", "file": ref, "channel": "default", "row_type": "fix-attribute"},
    )


def _age_task(task, *, days):
    EnrichmentTask.objects.filter(pk=task.pk).update(modified_at=timezone.now() - timedelta(days=days))


@pytest.mark.django_db
def test_prune_drops_staged_csv_of_aged_done_task(fake_adapter, settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    ref = staging_store.save(SimpleUploadedFile("f.csv", b"sku,field,type\nA,b,fix-attribute\n"))
    task = _csv_task(ref)
    _age_task(task, days=40)

    result = cleanup_service.prune(days=30)

    assert result["csv_staged_deleted"] == 1
    assert not staging_store.exists(ref)
    assert not EnrichmentTask.objects.filter(pk=task.pk).exists()


@pytest.mark.django_db
def test_prune_keeps_staged_csv_referenced_by_live_task(fake_adapter, settings, tmp_path):
    # One upload → N tasks share a file. A slower work-type still OPEN must keep the file alive.
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    ref = staging_store.save(SimpleUploadedFile("f.csv", b"sku,field,type\nA,b,fix-attribute\n"))
    done = _csv_task(ref, status=TaskStatus.DONE.value)
    open_task = _csv_task(ref, status=TaskStatus.OPEN.value)
    _age_task(done, days=40)

    result = cleanup_service.prune(days=30)

    assert result["csv_staged_deleted"] == 0
    assert staging_store.exists(ref)  # guarded — the open task still streams from it
    assert EnrichmentTask.objects.filter(pk=open_task.pk).exists()
