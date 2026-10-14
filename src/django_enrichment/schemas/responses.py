# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Pydantic response schemas for the enrichment admin API (etap-05).

`from_attributes=True` lets a ViewSet serialise an ORM instance directly. Per the API response
contract every field is always present (null when empty) — the field set never changes, only
values do. Standard pagination shape (count/next/previous/results) for both resources (PO decision).
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    status: str
    scope_spec: dict
    params: dict
    batch_key: str
    counts: dict
    errors: list[dict]
    # {proposal status: n} of the task's proposals — filled by the list/retrieve views, not an ORM attribute.
    proposal_counts: dict[str, int] = {}
    requested_by_id: int | None
    created_at: datetime
    modified_at: datetime


class TaskListResponse(BaseModel):
    count: int
    next: str | None
    previous: str | None
    results: list[TaskResponse]


class TargetsResponse(BaseModel):
    """Lazily-resolved targets for a worker pull. `targets` are adapter-specific (PIM: SKUs)."""

    task_id: int
    page: int
    targets: list[Any]


class CSVImportResponse(BaseModel):
    """`POST /tasks/import-csv/` result (etap-11). One file → N tasks (grouped by the CSV `type`).

    `staged_file` is the shared staging ref every task streams from; `tasks` is the spawned set
    (one per distinct work-type). Every field always present per the API response contract."""

    staged_file: str
    task_count: int
    tasks: list[TaskResponse]


class ProposalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int | None
    target_module: str
    target_type: str
    subject_ref: str
    subject_label: str
    subject_url: str
    target_kind: str
    target_locator: dict
    locator_hash: str
    proposed_value: dict
    staged_file: str
    current_snapshot: dict
    applied_snapshot: dict
    status: str
    source: str
    confidence: Decimal | None
    batch_id: str
    external_ref: str
    reviewed_by_id: int | None
    reviewed_at: datetime | None
    applied_at: datetime | None
    reject_reason: str
    rules: list[str]
    created_at: datetime
    modified_at: datetime


class ProposalListResponse(BaseModel):
    count: int
    next: str | None
    previous: str | None
    results: list[ProposalResponse]


class BulkAcceptResponse(BaseModel):
    """Routing-aware bulk apply result (etap-09). `mode` is `sync` (counts populated, `enqueued` null) or
    `async` (`enqueued` set, counts null) — every field always present per the API response contract."""

    mode: str = Field(description="'sync' (applied in-request) or 'async' (dispatched to the Celery canvas)")
    enqueued: int | None = Field(description="Proposals dispatched to async apply; null when sync")
    applied: list[int] | None = Field(description="Applied proposal ids; null when async")
    drifted: list[int] | None = Field(description="Drifted (not written) proposal ids; null when async")
    failed: list[int] | None = Field(description="Failed proposal ids; null when async")


class BulkUndoResponse(BaseModel):
    """Routing-aware bulk undo result (etap-09). Same shape as `BulkAcceptResponse` for undo buckets."""

    mode: str = Field(description="'sync' (reverted in-request) or 'async' (dispatched to the Celery canvas)")
    enqueued: int | None = Field(description="Proposals dispatched to async undo; null when sync")
    reverted: list[int] | None = Field(description="Reverted proposal ids; null when async")
    blocked: list[int] | None = Field(description="Drift-blocked proposal ids (stayed applied); null when async")
    failed: list[int] | None = Field(description="Failed proposal ids; null when async")


class BulkRejectResponse(BaseModel):
    rejected: int = Field(description="Number of pending proposals rejected")


class SpawnRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    module: str
    check_key: str
    params: dict
    scope: dict
    task_type: str
    task_params: dict
    limit: int | None
    cooldown_days: int | None
    auto: bool
    active: bool
    created_at: datetime
    modified_at: datetime


class SpawnRuleListResponse(BaseModel):
    count: int
    next: str | None
    previous: str | None
    results: list[SpawnRuleResponse]


class SpawnRuleRunResponse(BaseModel):
    """`POST /spawn-rules/{key}/run/` — `spawned` (201, fresh task) | `already_running` (200,
    the rule's open/in_progress task) | `no_candidates` (200, probe came back empty)."""

    status: str = Field(examples=["spawned", "already_running", "no_candidates"])
    task: TaskResponse | None = None


class WorkerResponse(BaseModel):
    """`GET /workers/` item — an active worker the CMS can spawn tasks for (`type` = `key`). The
    endpoint returns a plain list (a handful of workers per module, no pagination)."""

    key: str = Field(description="Worker key = task type", examples=["pim.translate"])
    label: str = Field(description="Human label", examples=["Translate descriptions"])
    kind: str = Field(description="Kind of work", examples=["translate"])
    target_module: str = Field(description="Adapter key", examples=["pim"])
    mode: str = Field(description="internal | external", examples=["internal"])
    params_schema: dict = Field(description="JSON schema of the task params", examples=[{"type": "object"}])


class RevisionResponse(BaseModel):
    """One version of a proposal's value (`GET proposals/{id}/revisions/` item, revise/restore result)."""

    model_config = ConfigDict(from_attributes=True)

    number: int = Field(examples=[2])
    value: dict = Field(examples=[{"text": "Shorter copy."}])
    instruction: str = Field(examples=["Make the text noticeably shorter; keep the key information."])
    scope: dict = Field(examples=[{"type": "all"}])
    author_kind: str = Field(description="ai | user | restore", examples=["ai"])
    author: int | None = Field(validation_alias="author_id", description="User id", examples=[1])
    created_at: datetime


class VariantsResponse(BaseModel):
    """`POST proposals/{id}/revise/` with `variants` — candidate values, nothing saved."""

    candidates: list[dict] = Field(examples=[[{"text": "Variant A"}, {"text": "Variant B"}]])


class RulePromoteResponse(BaseModel):
    target: str = Field(examples=["worker"])
    rules: list[str] = Field(
        description="The target's rules after the promotion", examples=[["Never mention the price."]]
    )


class TaskReviseResponse(BaseModel):
    queued: int = Field(description="Pending proposals queued for correction", examples=[12])
