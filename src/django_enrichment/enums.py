# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models


class TaskStatus(models.TextChoices):
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In progress"
    DONE = "done", "Done"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"


class ProposalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPLIED = "applied", "Applied"
    REJECTED = "rejected", "Rejected"
    SUPERSEDED = "superseded", "Superseded"
    DRIFTED = "drifted", "Drifted"
    REVERTED = "reverted", "Reverted"


# State machine as data (django-suppliers review_service pattern). Transition LOGIC
# lives in the service layer (etap-03); these tables only declare the allowed edges so
# the models stay logic-free and the model-layer tests can assert against them.

# D3 lifecycle: open -> in_progress -> done | cancelled | failed
TASK_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    TaskStatus.OPEN.value: frozenset({TaskStatus.IN_PROGRESS.value, TaskStatus.CANCELLED.value}),
    TaskStatus.IN_PROGRESS.value: frozenset(
        {TaskStatus.DONE.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
    ),
    TaskStatus.DONE.value: frozenset(),
    TaskStatus.CANCELLED.value: frozenset(),
    TaskStatus.FAILED.value: frozenset(),
}

# KISS (D2): pending -> applied | rejected | superseded | drifted ; drifted -> applied | rejected
# Single-level undo (etap-09): applied -> reverted (terminal). One step back only — deeper history is
# django-history's job (separate track). `reverted` is terminal: re-applying means a fresh proposal.
PROPOSAL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    ProposalStatus.PENDING.value: frozenset(
        {
            ProposalStatus.APPLIED.value,
            ProposalStatus.REJECTED.value,
            ProposalStatus.SUPERSEDED.value,
            ProposalStatus.DRIFTED.value,
        }
    ),
    ProposalStatus.DRIFTED.value: frozenset({ProposalStatus.APPLIED.value, ProposalStatus.REJECTED.value}),
    ProposalStatus.APPLIED.value: frozenset({ProposalStatus.REVERTED.value}),
    ProposalStatus.REJECTED.value: frozenset(),
    ProposalStatus.SUPERSEDED.value: frozenset(),
    ProposalStatus.REVERTED.value: frozenset(),
}
