# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Where a proposal belongs (a leaf: no service imports)."""

from django_enrichment.models import ContentProposal


def channel_of(proposal: ContentProposal) -> str:
    """The proposal's channel: its locator's, else its task's `params.channel`, else blank (the instance)."""
    params = (proposal.task.params if proposal.task else None) or {}
    return str((proposal.target_locator or {}).get("channel") or params.get("channel") or "")
