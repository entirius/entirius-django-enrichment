# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Proposal service — intake / review / accept / reject / bulk (framework-agnostic).

Intake is authoritative about `current_snapshot`: it reads the live value through the adapter
rather than trusting a worker-supplied "before" (drift detection needs ground truth). A new
proposal supersedes any older `pending` proposal on the same target. Accept routes through
`apply_service` (drift-aware write); reject is a cheap status flip. The state machine
(`PROPOSAL_STATUS_TRANSITIONS`) is enforced here — the model carries no logic.

Pattern mirrors `django-suppliers/services/review_service.py` (transitions + bulk `.update()`).
"""

import logging
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.db.models import QuerySet
from django.utils import timezone

from django_enrichment import settings as enrichment_settings
from django_enrichment.adapters import get_adapter
from django_enrichment.enums import PROPOSAL_STATUS_TRANSITIONS, ProposalStatus
from django_enrichment.models import ContentProposal, EnrichmentTask, compute_locator_hash
from django_enrichment.services import apply_service, staging_store, task_service

_logger = logging.getLogger("process")


def transition_status(
    proposal: ContentProposal, new_status: str, *, user: AbstractBaseUser | None = None, reason: str = ""
) -> ContentProposal:
    """Validate and apply a status transition, stamping audit fields per target status."""
    allowed = PROPOSAL_STATUS_TRANSITIONS.get(proposal.status)
    if allowed is None or new_status not in allowed:
        raise ValueError(f"invalid proposal transition: {proposal.status} -> {new_status}")

    update_fields = ["status", "modified_at"]
    proposal.status = new_status

    if new_status == ProposalStatus.APPLIED.value:
        proposal.applied_at = timezone.now()
        proposal.reviewed_by = user
        proposal.reviewed_at = timezone.now()
        update_fields += ["applied_at", "reviewed_by", "reviewed_at"]
    elif new_status == ProposalStatus.REJECTED.value:
        proposal.reviewed_by = user
        proposal.reviewed_at = timezone.now()
        proposal.reject_reason = reason
        update_fields += ["reviewed_by", "reviewed_at", "reject_reason"]
    elif new_status == ProposalStatus.REVERTED.value:
        # Single-level undo (etap-09): stamp who reverted and when. `applied_at` is left intact — the
        # proposal *was* applied; this is history-lite, not a deletion of that fact.
        proposal.reviewed_by = user
        proposal.reviewed_at = timezone.now()
        update_fields += ["reviewed_by", "reviewed_at"]

    proposal.save(update_fields=update_fields)
    return proposal


def intake(
    *,
    target_module: str,
    subject_ref: str,
    target_kind: str,
    target_locator: dict,
    proposed_value: dict,
    task: EnrichmentTask | None = None,
    target_type: str = "",
    subject_label: str = "",
    subject_url: str = "",
    source: str = "",
    confidence: Decimal | None = None,
    batch_id: str = "",
    external_ref: str = "",
    staged_file: str = "",
) -> ContentProposal:
    """Ingest one worker proposal: idempotent on `external_ref`, snapshot live, supersede older."""
    if external_ref:
        existing = ContentProposal.objects.filter(external_ref=external_ref).first()
        if existing is not None:
            return existing

    # Authoritative snapshot — never trust the worker's "before".
    current_snapshot = get_adapter(target_module).read_current(
        subject_ref=subject_ref, target_kind=target_kind, target_locator=target_locator
    )

    _supersede_pending(target_module, subject_ref, target_kind, target_locator)

    proposal = ContentProposal.objects.create(
        task=task,
        target_module=target_module,
        target_type=target_type,
        subject_ref=subject_ref,
        subject_label=subject_label,
        subject_url=subject_url,
        target_kind=target_kind,
        target_locator=target_locator,
        proposed_value=proposed_value,
        current_snapshot=current_snapshot,
        source=source,
        confidence=confidence,
        batch_id=batch_id,
        external_ref=external_ref,
        staged_file=staged_file,
    )
    if task is not None:
        task_service.increment_count(task, "proposed")
    return proposal


def _sniff_image_mime(head: bytes) -> str | None:
    """Detect image type from leading magic bytes — authoritative, never the client's declared type.

    Trusting the multipart `content_type` would let a worker (or a leaked key) stage an executable
    behind an `image/png` label. Sniffing the actual bytes is the security-correct gate.
    """
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and b"avif" in head[8:20]:
        return "image/avif"
    return None


def _validate_media(file) -> None:
    """Boundary validation for a staged image — size cap + magic-byte MIME allowlist. Raises ValueError.

    The service owns this (not just the view): a management command or bridge that calls
    `intake_media` directly gets the same guarantee.
    """
    # django.conf.settings checked first so a host override / test fixture wins; the module-settings
    # value is the single source of the literal defaults (no duplicated constants here).
    max_bytes = getattr(settings, "ENRICHMENT_MAX_IMAGE_BYTES", enrichment_settings.ENRICHMENT_MAX_IMAGE_BYTES)
    allowed = getattr(settings, "ENRICHMENT_ALLOWED_IMAGE_MIME", enrichment_settings.ENRICHMENT_ALLOWED_IMAGE_MIME)
    size = getattr(file, "size", None)
    if size is not None and size > max_bytes:
        raise ValueError(f"file exceeds the {max_bytes}-byte cap")
    file.seek(0)
    head = file.read(32)
    file.seek(0)
    mime = _sniff_image_mime(head)
    if mime is None or mime not in allowed:
        raise ValueError(f"unsupported image type (allowed: {sorted(allowed)})")


def intake_media(
    *,
    file,
    target_module: str,
    subject_ref: str,
    target_locator: dict,
    proposed_value: dict,
    task: EnrichmentTask | None = None,
    target_type: str = "",
    subject_label: str = "",
    subject_url: str = "",
    source: str = "",
    confidence: Decimal | None = None,
    batch_id: str = "",
    external_ref: str = "",
) -> ContentProposal:
    """Intake a media proposal: validate + stage the binary, then create the proposal (etap-08).

    The binary lands in the bus's staging-store (NOT the target module). `target_kind` is forced to
    `"picture"`. Cleans up the staged file if proposal creation fails, or if `external_ref`
    idempotency returns a pre-existing proposal (our freshly staged copy would otherwise orphan).
    """
    _validate_media(file)
    ref = staging_store.save(file)
    try:
        proposal = intake(
            target_module=target_module,
            subject_ref=subject_ref,
            target_kind="picture",
            target_locator=target_locator,
            proposed_value=proposed_value,
            task=task,
            target_type=target_type,
            subject_label=subject_label,
            subject_url=subject_url,
            source=source,
            confidence=confidence,
            batch_id=batch_id,
            external_ref=external_ref,
            staged_file=ref,
        )
    except Exception:
        staging_store.delete(ref)
        raise
    if proposal.staged_file != ref:
        # Idempotent re-submit returned an existing proposal — drop our orphaned staged copy.
        staging_store.delete(ref)
    return proposal


def _supersede_pending(target_module: str, subject_ref: str, target_kind: str, target_locator: dict) -> None:
    """Mark older pending proposals on the same target as superseded.

    Single `.update()`, not per-row `transition_status`: superseding is a pure status flip with no
    audit, the filter guarantees the source is `pending`, and `pending → superseded` is always legal.
    `locator_hash` computed explicitly (the model `save()` is bypassed by `.update()`).
    """
    locator_hash = compute_locator_hash(target_locator)
    ContentProposal.objects.filter(
        target_module=target_module,
        subject_ref=subject_ref,
        target_kind=target_kind,
        locator_hash=locator_hash,
        status=ProposalStatus.PENDING.value,
    ).update(status=ProposalStatus.SUPERSEDED.value, modified_at=timezone.now())


def list_for_review(
    *,
    status: str = ProposalStatus.PENDING.value,
    module: str | None = None,
    target_kind: str | None = None,
    batch_id: str | None = None,
    source: str | None = None,
    confidence_min: Decimal | None = None,
    search: str | None = None,
    ordering: str = "-created_at",
) -> QuerySet[ContentProposal]:
    """Server-side filtered/ordered review queue."""
    qs = ContentProposal.objects.all()
    if status is not None:
        qs = qs.filter(status=status)
    if module is not None:
        qs = qs.filter(target_module=module)
    if target_kind is not None:
        qs = qs.filter(target_kind=target_kind)
    if batch_id is not None:
        qs = qs.filter(batch_id=batch_id)
    if source is not None:
        qs = qs.filter(source=source)
    if confidence_min is not None:
        qs = qs.filter(confidence__gte=confidence_min)
    if search:
        qs = qs.filter(subject_label__icontains=search)
    return qs.order_by(ordering)


def get_proposal(proposal_id: int) -> ContentProposal:
    """Fetch a proposal by pk. Raises `ValueError(... not found)` so the API maps it to 404 without
    importing the model into the view layer."""
    try:
        return ContentProposal.objects.get(pk=proposal_id)
    except ContentProposal.DoesNotExist as exc:
        raise ValueError(f"Proposal {proposal_id} not found") from exc


def accept(proposal: ContentProposal, *, user: AbstractBaseUser | None = None) -> ContentProposal:
    """Accept a single proposal — drift-aware write via the adapter."""
    return apply_service.apply(proposal, user=user)


def reject(proposal: ContentProposal, reason: str, *, user: AbstractBaseUser | None = None) -> ContentProposal:
    # Reject deletes the staged binary → the target module has zero trace (etap-08, decision #8).
    if proposal.staged_file:
        staging_store.delete(proposal.staged_file)
    return transition_status(proposal, ProposalStatus.REJECTED.value, user=user, reason=reason)


def apply_many(proposal_ids: list[int], *, user_id: int | None = None) -> dict[str, Any]:
    """Synchronous apply core (called by each Celery chunk and directly by tests).

    Per-row drift-aware apply; a drifted proposal is reported, not retried. Buckets the outcome
    so the caller (and the worker log) sees applied / drifted / failed counts. Idempotent on re-run:
    an already-`applied` proposal is rejected by `apply_service.apply` (status guard) → `failed` bucket,
    never a double-write. `.iterator()` keeps a 10k-id chunk from caching the whole result set (OOM, R3).
    """
    user = _resolve_user(user_id)
    applied: list[int] = []
    drifted: list[int] = []
    failed: list[int] = []
    for proposal in ContentProposal.objects.filter(pk__in=proposal_ids).iterator():
        try:
            result = apply_service.apply(proposal, user=user)
        except ValueError:
            # Status guard rejected an ineligible proposal (e.g. already applied on an idempotent
            # re-run) — benign, not an error. Bucket as failed but skip the traceback.
            _logger.info("apply_many skipped proposal %s (not applyable)", proposal.pk)
            failed.append(proposal.pk)
            continue
        except Exception:  # noqa: BLE001 — per-row isolation: one opaque adapter write must not abort the batch
            _logger.exception("apply_many failed for proposal %s", proposal.pk)
            failed.append(proposal.pk)
            continue
        (drifted if result.status == ProposalStatus.DRIFTED.value else applied).append(proposal.pk)
    return {"applied": applied, "drifted": drifted, "failed": failed}


def revert_many(proposal_ids: list[int], *, user_id: int | None = None) -> dict[str, Any]:
    """Synchronous undo core (called by each Celery chunk and directly by tests) — mirror of `apply_many`.

    Per-row drift-aware revert via `undo_service`. An item whose live value drifted from `applied_snapshot`
    (post-apply edit by someone else) lands in `blocked` and stays `applied`; a non-`applied` proposal or
    adapter error lands in `failed`. `.iterator()` bounds memory the same way `apply_many` does.
    """
    from django_enrichment.services import undo_service  # local import: avoids service import cycle

    user = _resolve_user(user_id)
    reverted: list[int] = []
    blocked: list[int] = []
    failed: list[int] = []
    for proposal in ContentProposal.objects.filter(pk__in=proposal_ids).iterator():
        try:
            undo_service.revert(proposal, user=user)
        except undo_service.UndoBlockedError:
            blocked.append(proposal.pk)
        except ValueError:
            # Status guard rejected a non-applied proposal (e.g. already reverted on a re-run) —
            # benign, not an error. Bucket as failed but skip the traceback.
            _logger.info("revert_many skipped proposal %s (not applied)", proposal.pk)
            failed.append(proposal.pk)
        except Exception:  # noqa: BLE001 — per-row isolation: one opaque adapter revert must not abort the batch
            _logger.exception("revert_many failed for proposal %s", proposal.pk)
            failed.append(proposal.pk)
        else:
            reverted.append(proposal.pk)
    return {"reverted": reverted, "blocked": blocked, "failed": failed}


def _async_threshold() -> int:
    """Batch size above which bulk operations leave the request cycle for the Celery canvas (PO etap-09)."""
    return getattr(settings, "ENRICHMENT_ASYNC_APPLY_THRESHOLD", enrichment_settings.ENRICHMENT_ASYNC_APPLY_THRESHOLD)


def bulk_accept(filters: dict[str, Any], *, user: AbstractBaseUser | None = None) -> dict[str, Any]:
    """Apply the filtered PENDING set — sync in-request when small, Celery canvas when large (PO etap-09).

    `≤ ENRICHMENT_ASYNC_APPLY_THRESHOLD` proposals apply synchronously so the operator gets immediate
    applied/drifted/failed counts; a larger set fans out over the canvas (chunks/chords) to avoid OOM.
    Constrained to `pending` (symmetry with `bulk_reject`): drifted proposals need the conscious per-item
    re-confirm view (D2), never silent bulk auto-confirm.

    Response carries every field (api-response-contract): `mode` + `enqueued` describe routing, the count
    lists are populated for sync and null for async (the worker logs the canvas totals).
    """
    qs = list_for_review(**filters).filter(status=ProposalStatus.PENDING.value)
    ids = list(qs.values_list("id", flat=True))
    user_id = getattr(user, "id", None)

    if len(ids) <= _async_threshold():
        result = apply_many(ids, user_id=user_id)
        return {"mode": "sync", "enqueued": None, **result}

    from django_enrichment.tasks.apply_tasks import dispatch_canvas

    dispatch_canvas(ids, user_id=user_id, op="apply")
    return {"mode": "async", "enqueued": len(ids), "applied": None, "drifted": None, "failed": None}


def bulk_undo(filters: dict[str, Any], *, user: AbstractBaseUser | None = None) -> dict[str, Any]:
    """Revert the filtered APPLIED set — same sync/canvas routing as `bulk_accept` (PO etap-09, bulk-only undo).

    Targets `applied` proposals in the filter. Each is drift-checked per item (`revert_many`): an item
    edited since apply lands in `blocked` (stays applied), never silently clobbered.
    """
    # `list_for_review` defaults `status` to pending; undo targets the applied set, so override it here
    # (the request filter set never carries `status`, so this is the single source of the status).
    qs = list_for_review(status=ProposalStatus.APPLIED.value, **filters)
    ids = list(qs.values_list("id", flat=True))
    user_id = getattr(user, "id", None)

    if len(ids) <= _async_threshold():
        result = revert_many(ids, user_id=user_id)
        return {"mode": "sync", "enqueued": None, **result}

    from django_enrichment.tasks.apply_tasks import dispatch_canvas

    dispatch_canvas(ids, user_id=user_id, op="revert")
    return {"mode": "async", "enqueued": len(ids), "reverted": None, "blocked": None, "failed": None}


def bulk_reject(filters: dict[str, Any], reason: str, *, user: AbstractBaseUser | None = None) -> dict[str, Any]:
    """Synchronous bulk reject — no adapter write, so a single `.update()` is enough (perf)."""
    qs = list_for_review(**filters).filter(status=ProposalStatus.PENDING.value)
    # One scan for both ids and staged refs (staged refs cleaned after the update so media rejects
    # leave no orphaned binaries; text-only rows simply have an empty staged_file).
    rows = list(qs.values_list("id", "staged_file"))
    ids = [pk for pk, _ in rows]
    staged_refs = [ref for _, ref in rows if ref]
    if ids:
        ContentProposal.objects.filter(pk__in=ids).update(
            status=ProposalStatus.REJECTED.value,
            reject_reason=reason,
            reviewed_by=user,
            reviewed_at=timezone.now(),
            modified_at=timezone.now(),
        )
    for ref in staged_refs:
        staging_store.delete(ref)
    return {"rejected": len(ids)}


def _resolve_user(user_id: int | None) -> AbstractBaseUser | None:
    if user_id is None:
        return None
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_id).first()
