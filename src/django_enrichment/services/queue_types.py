# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Types of the work queue: the lease error and the per-subject result a worker reports."""

from dataclasses import dataclass, field

from django_enrichment.kinds import ProposalDraft


class LeaseLost(Exception):
    """The token is unknown, expired or already given to someone else — the caller must stop working on the unit."""

    code = "LEASE_LOST"


@dataclass
class SubjectResult:
    """What a worker reports for one subject of a unit: drafts, or an `error_code` (+ `error_id` of its log trace;
    `error_exception` = the class name of an `INTERNAL_ERROR`, internal consumer only)."""

    subject_ref: str
    drafts: list[ProposalDraft] = field(default_factory=list)
    error_code: str = ""
    error_id: str = ""
    error_message: str = ""
    error_exception: str = ""
