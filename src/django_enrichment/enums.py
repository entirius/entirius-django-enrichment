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


class TaskLane(models.TextChoices):
    """Which Celery lane runs an internal task: `now` = the interactive lane, `queue` = the bulk lane."""

    NOW = "now", "Run now"
    QUEUE = "queue", "Queue"


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


class WorkerMode(models.TextChoices):
    INTERNAL = "internal", "Internal (our runner)"
    EXTERNAL = "external", "External (n8n etc. via the API)"


class RevisionAuthor(models.TextChoices):
    AI = "ai", "AI"
    USER = "user", "User"
    RESTORE = "restore", "Restore"


# Proposals that still accept new revisions (correction, manual edit, restore).
REVISABLE_STATUSES = frozenset({ProposalStatus.PENDING.value, ProposalStatus.DRIFTED.value})


class RunStatus(models.TextChoices):
    """`WorkflowRun.status` — derived from its steps by `workflow_service` (cancelled is final)."""

    RUNNING = "running", "Running"
    WAITING = "waiting", "Waiting for review"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class StepStatus(models.TextChoices):
    """One step of a run: waiting (needs not approved yet) → running → review → approved | failed."""

    WAITING = "waiting", "Waiting"
    RUNNING = "running", "Running"
    REVIEW = "review", "Needs review"
    APPROVED = "approved", "Approved"
    FAILED = "failed", "Failed"


class StepGate(models.TextChoices):
    REVIEW = "review", "Operator approves"
    AUTO = "auto", "Approved automatically"


class StepOutput(models.TextChoices):
    """What a step produces; each output is made by one kind (`STEP_OUTPUT_KINDS`)."""

    ARTIFACT = "artifact", "Artifact (text or data)"
    QUESTIONS = "questions", "Questions for the operator"
    DOCUMENT = "document", "Document in the target module"


STEP_OUTPUT_KINDS: dict[str, str] = {
    StepOutput.ARTIFACT.value: "generate",
    StepOutput.QUESTIONS.value: "questions",
    StepOutput.DOCUMENT.value: "compose",
}


class MemoryKind(models.TextChoices):
    """`MemoryEntry.kind` (contract A4 § 3)."""

    SEO_BRIEF = "seo_brief", "SEO brief"
    ANSWER = "answer", "Operator answer"
    RULE_SUGGESTION = "rule_suggestion", "Rule suggestion"
    FACT_SUGGESTION = "fact_suggestion", "Fact suggestion"


class MemoryStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DISMISSED = "dismissed", "Dismissed"
    PROMOTED = "promoted", "Promoted to the profile"
