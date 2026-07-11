# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Undo service — single-level revert of an applied proposal (drift-aware, module-agnostic).

The mirror image of `apply_service`: `apply` is the only write into a source module, `revert` is the
only way back. It routes by `proposal.target_module` to the same adapter's `revert()` — there is no
`if target_module == "pim"` here either.

Single-level only (decision #9): one step back, to the state captured in `current_snapshot` before the
apply. Deeper history is `django-history`'s job (separate track). `applied → reverted` is terminal —
re-applying means a fresh proposal.

Drift contract for undo (decision PO etap-09): before reverting we re-read the live value and compare it
to `applied_snapshot` (the post-apply truth `apply_service` recorded). If they diverge, someone edited
the target after our apply — we raise `UndoBlockedError` and write nothing rather than silently clobber
their change. The PIM picture revert anchors on the OLD `Picture` (kept in the pool until the etap-10
cleanup beat GC-s it), so undo expires with retention.
"""

import logging

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction

from django_enrichment.adapters import get_adapter
from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal

_logger = logging.getLogger("process")


class UndoBlockedError(Exception):
    """The live value drifted from `applied_snapshot` — the target changed after apply, so undo refuses."""


def revert(proposal: ContentProposal, *, user: AbstractBaseUser | None = None) -> ContentProposal:
    """Drift-check then restore `current_snapshot` through the target module's adapter.

    Raises `ValueError` if the proposal is not `applied`, `UndoBlockedError` if the live value no longer
    matches what we wrote (post-apply edit by someone else).
    """
    from django_enrichment.services import proposal_service  # local import: avoids service import cycle

    if proposal.status != ProposalStatus.APPLIED.value:
        raise ValueError(f"cannot undo proposal in status {proposal.status!r} (only 'applied')")

    adapter = get_adapter(proposal.target_module)
    live = adapter.read_current(
        subject_ref=proposal.subject_ref, target_kind=proposal.target_kind, target_locator=proposal.target_locator
    )
    if live != proposal.applied_snapshot:
        # Someone changed the target after our apply — undoing would clobber their edit. Refuse.
        raise UndoBlockedError(f"proposal {proposal.pk} target changed since apply; undo would overwrite a newer value")

    # Atomic so the module revert and the status flip commit together (same crash-safety reasoning as
    # apply_service.apply — a crash between them would leave the source restored but the proposal still
    # 'applied', inviting a double-undo).
    with transaction.atomic():
        adapter.revert(proposal)
        return proposal_service.transition_status(proposal, ProposalStatus.REVERTED.value, user=user)
