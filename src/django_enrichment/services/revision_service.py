# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Revision service — the only writer of `ProposalRevision` and of a revised `proposed_value`.

Invariant: `proposal.proposed_value == value of the latest revision`. Intake records revision 1;
every later revision (AI correction, manual edit, restore) goes through `add_revision`, which writes
the revision and the proposal in one transaction. Only `pending` / `drifted` proposals are revisable.
"""

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Max, QuerySet

from django_enrichment.enums import REVISABLE_STATUSES, RevisionAuthor
from django_enrichment.models import ContentProposal, ProposalRevision

MEDIA_KINDS = frozenset({"picture", "video"})


def record_initial(proposal: ContentProposal) -> ProposalRevision:
    """Revision 1 of a freshly ingested proposal (`ai`, no instruction) — called by intake."""
    return ProposalRevision.objects.create(
        proposal=proposal, number=1, value=proposal.proposed_value, author_kind=RevisionAuthor.AI.value
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
) -> ProposalRevision:
    """Append the next revision and make it the proposal's value — one transaction, row-locked."""
    ensure_revisable(proposal)
    with transaction.atomic():
        ContentProposal.objects.select_for_update().filter(pk=proposal.pk).first()
        revision = ProposalRevision.objects.create(
            proposal=proposal,
            number=_next_number(proposal),
            value=value,
            instruction=instruction,
            scope=scope or {},
            author_kind=author_kind,
            author=author,
        )
        proposal.proposed_value = value
        proposal.save(update_fields=["proposed_value", "modified_at"])
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
