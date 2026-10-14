# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel

from django_enrichment.enums import RevisionAuthor


class ProposalRevision(BaseModel):
    """One version of a proposal's value (1-based `number`). The latest one equals `proposed_value`.

    Written only by `revision_service` (which keeps that invariant); `value` has the shape of
    `proposed_value`, `scope` records what part of the text a correction targeted.
    """

    proposal = models.ForeignKey(
        "django_enrichment.ContentProposal", on_delete=models.CASCADE, related_name="revisions"
    )
    number = models.PositiveIntegerField()
    value = models.JSONField(default=dict, blank=True)
    instruction = models.TextField(blank=True, default="")
    scope = models.JSONField(default=dict, blank=True)
    author_kind = models.CharField(max_length=16, choices=RevisionAuthor.choices)
    author = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Cost of the completion behind this revision (revision 1 = the proposal's cost); null = unknown or no call.
    cost_usd = models.DecimalField(max_digits=14, decimal_places=8, null=True, blank=True)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["proposal", "number"], name="enr_revision_proposal_number_uniq"),
        ]

    def __str__(self) -> str:
        return f"ProposalRevision(proposal={self.proposal_id}, #{self.number} {self.author_kind})"
