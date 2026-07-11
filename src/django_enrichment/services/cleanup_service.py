# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Cleanup service — retention pruning (etap-10). One knob `X` days rules undo + retention.

The bus keeps undo anchors alive forever: applied proposals stay revertable, their displaced PIM
`Picture` pinned in the pool, staged binaries (for superseded media) never dropped. Left alone the
`ContentProposal` table and the staging store grow with the catalogue, not with recent activity.

`prune(days)` settles that: it deletes proposals in a *terminal* status older than `X` days plus
`done` tasks older than `X`, drops each pruned media proposal's staged file, and asks the target
module's adapter to GC the orphaned undo anchor (the PIM adapter deletes the displaced `Picture`
only when nothing else references it — SHA1 dedup is shared). `pending` is never touched; that's
live review work. After `X` days undo expires and the way back is gone — the live value stays in the
source module, which is the point.

Module-agnostic (decision D1): the anchor GC is delegated to `adapter.release_undo_anchor` via the
registry — no `if target_module == "pim"` here, no knowledge of `Picture`. Idempotent and re-run
safe: a second pass finds nothing, `staging_store.delete` no-ops on a missing ref, and the adapter's
GC is reference-guarded.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from django_enrichment import settings as enrichment_settings
from django_enrichment.adapters import get_adapter
from django_enrichment.enums import ProposalStatus, TaskStatus
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import staging_store

_logger = logging.getLogger("process")

# The media `target_kind` (etap-08). Only picture proposals carry a staged file or an external undo
# anchor, so only they need the per-row side-effect pass — the text majority is bulk-deleted.
_PICTURE_KIND = "picture"

# Modules we've already warned about being unregistered — so a backlog of proposals for a removed
# module logs once, not once per row.
_warned_missing_modules: set[str] = set()

# Terminal proposal statuses aged by `modified_at` (the time they entered the terminal state — they
# are never re-saved afterwards). `applied` is handled separately: aged by `applied_at`, which the
# `(status, applied_at)` index serves directly. `pending` is deliberately absent — never auto-pruned.
_OTHER_TERMINAL = (
    ProposalStatus.REJECTED.value,
    ProposalStatus.SUPERSEDED.value,
    ProposalStatus.DRIFTED.value,
    ProposalStatus.REVERTED.value,
)


def _retention_days() -> int:
    return getattr(settings, "ENRICHMENT_RETENTION_DAYS", enrichment_settings.ENRICHMENT_RETENTION_DAYS)


def _aged_proposals(cutoff):
    """Proposals in a terminal status older than `cutoff` (applied by `applied_at`, rest by `modified_at`)."""
    return ContentProposal.objects.filter(
        Q(status=ProposalStatus.APPLIED.value, applied_at__lt=cutoff)
        | Q(status__in=_OTHER_TERMINAL, modified_at__lt=cutoff)
    )


def _release_anchors_and_staged(aged) -> tuple[int, int]:
    """Per-row side effects for the media subset only: GC the undo anchor + drop the staged file.

    Returns `(pictures_gc, staged_deleted)`. Best-effort — a failing adapter or a missing staged
    file must never abort the prune (a half-cleaned row is just re-attempted next run). Streams with
    `.iterator()` so a large media backlog never materialises at once (OOM, R3).
    """
    pictures_gc = 0
    staged_deleted = 0
    chunk = enrichment_settings.ENRICHMENT_APPLY_BATCH_SIZE
    for proposal in aged.filter(target_kind=_PICTURE_KIND).iterator(chunk_size=chunk):
        adapter = _adapter_or_none(proposal.target_module)
        hook = getattr(adapter, "release_undo_anchor", None) if adapter is not None else None
        if hook is not None:
            try:
                pictures_gc += hook(proposal) or 0
            except Exception:  # noqa: BLE001 — cleanup is best-effort; log and keep going
                _logger.exception("release_undo_anchor failed for proposal %s", proposal.pk)
        if proposal.staged_file:
            staging_store.delete(proposal.staged_file)
            staged_deleted += 1
    return pictures_gc, staged_deleted


def _adapter_or_none(target_module: str) -> object | None:
    """`get_adapter` (itself cached by dotted path) returning None for an unregistered module.

    A proposal for a module no longer in `ENRICHMENT_ADAPTERS` must not abort the prune. Warns once
    per module, not once per row.
    """
    try:
        return get_adapter(target_module)
    except ValueError:
        if target_module not in _warned_missing_modules:
            _logger.warning("no adapter for target_module=%r; skipping undo-anchor GC", target_module)
            _warned_missing_modules.add(target_module)
        return None


def _drop_unreferenced_csv_files(refs: list[str]) -> int:
    """Drop each staged CSV ref whose tasks are all gone — guarded against a still-live referencer.

    One upload stages a single file shared by N tasks (one per work-type). A ref is safe to delete
    only once no `open`/`in_progress` task still points at it; a slower work-type left open keeps the
    file alive. Idempotent — `staging_store.delete` no-ops on an already-gone ref. Returns the count
    of files removed.
    """
    distinct = {r for r in refs if r}
    if not distinct:
        return 0
    # One IN query for every still-live ref, then filter locally — not one EXISTS per ref (a prune
    # that ages out hundreds of uploads would otherwise fan out hundreds of JSONB-path queries).
    live = set(
        EnrichmentTask.objects.filter(
            status__in=(TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value),
            scope_spec__mode="csv",
            scope_spec__file__in=list(distinct),
        ).values_list("scope_spec__file", flat=True)
    )
    deleted = 0
    for ref in distinct - live:
        staging_store.delete(ref)
        deleted += 1
    return deleted


def prune(days: int | None = None) -> dict[str, int]:
    """Delete terminal proposals + done tasks older than `days`, GC their anchors and staged files.

    `days` defaults to `ENRICHMENT_RETENTION_DAYS`. `days=0` prunes everything currently qualifying.
    Raises `ValueError` for a negative window. Returns counts for operator visibility / logs.
    """
    days = _retention_days() if days is None else days
    if days < 0:
        raise ValueError("days must be >= 0")
    cutoff = timezone.now() - timedelta(days=days)

    aged = _aged_proposals(cutoff)
    pictures_gc, staged_deleted = _release_anchors_and_staged(aged)
    deleted_proposals, _ = aged.delete()

    # A CSV import (etap-11) stages one file referenced by N tasks (one per work-type). Collect the
    # done-aged CSV refs BEFORE deleting their tasks, then drop each file unless another live task
    # still references it (a slower work-type still open) — the same shared-ref guard the media GC uses.
    done_aged = EnrichmentTask.objects.filter(status=TaskStatus.DONE.value, modified_at__lt=cutoff)
    csv_refs = list(done_aged.filter(scope_spec__mode="csv").values_list("scope_spec__file", flat=True))
    deleted_tasks, _ = done_aged.delete()
    csv_staged_deleted = _drop_unreferenced_csv_files(csv_refs)

    counts = {
        "deleted_proposals": deleted_proposals,
        "deleted_tasks": deleted_tasks,
        "pictures_gc": pictures_gc,
        "staged_deleted": staged_deleted,
        "csv_staged_deleted": csv_staged_deleted,
        "retention_days": days,
    }
    _logger.info("enrichment prune: %s", counts)
    return counts
