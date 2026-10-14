# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Revision service — the only writer of `ProposalRevision` and of a revised `proposed_value`.

Invariant: `proposal.proposed_value == value of the latest revision`. Intake records revision 1;
every later revision (AI correction, manual edit, restore) goes through `add_revision`, which writes
the revision and the proposal in one transaction. Only `pending` / `drifted` proposals are revisable.
"""

from decimal import Decimal

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Max, QuerySet

from django_enrichment.enums import REVISABLE_STATUSES, RevisionAuthor
from django_enrichment.models import ContentProposal, ProposalRevision
from django_enrichment.services.revise_values import RevisionNotApplicableError

MEDIA_KINDS = frozenset({"picture", "video"})


def record_initial(proposal: ContentProposal) -> ProposalRevision:
    """Revision 1 of a freshly ingested proposal (`ai`, no instruction) — called by intake."""
    return ProposalRevision.objects.create(
        proposal=proposal,
        number=1,
        value=proposal.proposed_value,
        author_kind=RevisionAuthor.AI.value,
        cost_usd=proposal.cost_usd,
    )


def ensure_revisable(proposal: ContentProposal) -> None:
    """Raise `ValueError` unless the proposal still accepts a new value (pending / drifted)."""
    if proposal.status not in REVISABLE_STATUSES:
        raise ValueError(f"only pending or drifted proposals can be revised (status: {proposal.status})")


def ensure_text_revisable(proposal: ContentProposal) -> None:
    """`ensure_revisable` plus: media proposals are not revisable by corrections in this package."""
    ensure_revisable(proposal)
    if proposal.target_kind in MEDIA_KINDS or proposal.staged_file:
        raise ValueError("media proposals cannot be corrected")


def add_revision(
    proposal: ContentProposal,
    value: dict,
    *,
    author_kind: str,
    author: AbstractBaseUser | None = None,
    instruction: str = "",
    scope: dict | None = None,
    base_value: dict | None = None,
    cost_usd: Decimal | None = None,
) -> ProposalRevision:
    """Append the next revision and make it the proposal's value — one transaction, row-locked.

    Status (and `base_value`, the value an AI answer was computed on) is re-checked under the lock, so a
    concurrent accept / edit is never overwritten. `cost_usd` = the completion behind an AI revision. Raises `ValueError` or `RevisionNotApplicableError`.
    The caller's `proposal` is refreshed from the database afterwards."""
    with transaction.atomic():
        locked = ContentProposal.objects.select_for_update().get(pk=proposal.pk)
        ensure_revisable(locked)
        if base_value is not None and locked.proposed_value != base_value:
            raise RevisionNotApplicableError("the value changed while the correction was running")
        revision = ProposalRevision.objects.create(
            proposal=locked,
            number=_next_number(locked),
            value=value,
            instruction=instruction,
            scope=scope or {},
            author_kind=author_kind,
            author=author,
            cost_usd=cost_usd,
        )
        locked.proposed_value = value
        locked.save(update_fields=["proposed_value", "modified_at"])
    proposal.refresh_from_db()
    return revision


def _next_number(proposal: ContentProposal) -> int:
    latest = proposal.revisions.aggregate(latest=Max("number"))["latest"]
    return (latest or 0) + 1


def list_revisions(proposal: ContentProposal) -> QuerySet[ProposalRevision]:
    return proposal.revisions.order_by("number")


def restore(proposal: ContentProposal, number: int, *, user: AbstractBaseUser | None = None) -> ProposalRevision:
    """A new `restore` revision copying revision `number`. Raises `ValueError(... not found)` for a miss."""
    source = proposal.revisions.filter(number=number).first()
    if source is None:
        raise ValueError(f"Revision {number} not found")
    return add_revision(
        proposal, source.value, author_kind=RevisionAuthor.RESTORE.value, author=user, instruction=f"#{number}"
    )
