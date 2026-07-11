# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Apply service — the bus's single write choke point (module-agnostic, drift-aware).

`apply` routes by `proposal.target_module` to the registered adapter's `apply()` — there is no
`if target_module == "pim"` anywhere. The drift contract (D2) lives here:

- `pending` proposal: re-read the live value; if it diverged from `current_snapshot`, mark the
  proposal `drifted` and DO NOT write (the CMS re-confirm view, etap-06, asks the operator).
- `drifted` proposal being accepted: the operator has re-confirmed, so refresh `current_snapshot`
  to the live value and write.

The adapter's `apply()` body for PIM lands in `django-pim` (etap-04). Until then the test fake
implements it in memory, so drift + apply are fully exercised at this stage.
"""

import logging

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from django_enrichment.adapters import get_adapter
from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal
from django_enrichment.services import staging_store

_logger = logging.getLogger("process")

_APPLYABLE_STATUSES = frozenset({ProposalStatus.PENDING.value, ProposalStatus.DRIFTED.value})


def _target_missing_error(proposal: ContentProposal) -> ValueError:
    """Adapter could not locate the proposal's target (deleted SKU / channel).

    Any adapter resolves the target by `subject_ref` + `target_locator` and raises Django's
    `ObjectDoesNotExist` (e.g. `Channel.DoesNotExist`) when it is gone. That is a normal operator
    situation — the SKU or channel was removed after the proposal was created — NOT a server fault,
    so the bus turns it into a readable `ValueError` (→ 400 via `raise_as_drf`) instead of letting
    the bare `DoesNotExist` surface as a 500.
    """
    channel = (proposal.target_locator or {}).get("channel")
    where = f"channel {channel!r}" if channel else "the target module"
    return ValueError(
        f"Cannot apply: SKU {proposal.subject_ref!r} on {where} no longer exists in "
        f"{proposal.target_module!r} (it may have been deleted) — nothing was written."
    )


def apply(proposal: ContentProposal, *, user: AbstractBaseUser | None = None) -> ContentProposal:
    """Drift-check then write `proposed_value` through the target module's adapter."""
    from django_enrichment.services import proposal_service  # local import: avoids service import cycle

    if proposal.status not in _APPLYABLE_STATUSES:
        raise ValueError(f"cannot apply proposal in status {proposal.status!r}")

    adapter = get_adapter(proposal.target_module)
    try:
        live = adapter.read_current(
            subject_ref=proposal.subject_ref, target_kind=proposal.target_kind, target_locator=proposal.target_locator
        )
    except ObjectDoesNotExist as exc:
        raise _target_missing_error(proposal) from exc

    if proposal.status == ProposalStatus.PENDING.value and live != proposal.current_snapshot:
        # Drift detected — bus refuses to clobber. Operator re-confirms via the CMS view (etap-06).
        proposal.current_snapshot = live
        proposal.save(update_fields=["current_snapshot", "modified_at"])
        return proposal_service.transition_status(proposal, ProposalStatus.DRIFTED.value, user=user)

    if proposal.status == ProposalStatus.DRIFTED.value:
        # Re-confirmed: snapshot the truth we are about to overwrite, then write.
        proposal.current_snapshot = live
        proposal.save(update_fields=["current_snapshot", "modified_at"])

    # Atomic so the module write and the status flip commit together — a crash between them
    # would leave the source mutated but the proposal still pending, risking a double-apply.
    # (In-process adapters share this transaction; an adapter doing external non-DB writes
    # cannot be rolled back — its own concern.)
    with transaction.atomic():
        try:
            adapter.apply(proposal)
        except ObjectDoesNotExist as exc:
            raise _target_missing_error(proposal) from exc
        # Capture the post-write live value as the undo drift anchor (etap-09). Re-reading through the
        # adapter (rather than assuming `proposed_value`) keeps this generic across target kinds — for a
        # picture `proposed_value` never holds the resulting sha1, but `read_current` does. Undo later
        # compares the live value against this; a mismatch means someone edited the target after us, so we
        # refuse to clobber their change.
        proposal.applied_snapshot = adapter.read_current(
            subject_ref=proposal.subject_ref, target_kind=proposal.target_kind, target_locator=proposal.target_locator
        )
        proposal.save(update_fields=["applied_snapshot", "modified_at"])
        result = proposal_service.transition_status(proposal, ProposalStatus.APPLIED.value, user=user)

    # The binary now lives in the target module (media — etap-08); drop the staged copy. The undo
    # anchor is the OLD source object (e.g. PIM Picture), never this file. Outside the transaction
    # (file ops aren't transactional) and best-effort — a stale staged file is harmless, cleaned by
    # the etap-10 beat; failing the apply over it would be wrong (the write already committed).
    if proposal.staged_file:
        try:
            staging_store.delete(proposal.staged_file)
        except Exception:  # noqa: BLE001 — staged cleanup must never fail a committed apply
            _logger.exception("staged-file cleanup failed for proposal %s", proposal.pk)
    return result
