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
    cost_usd: Decimal | None = Field(
        description="Sum of the known toolbox costs; null = none known", examples=["0.0042"]
    )
    cost_breakdown: dict[str, Decimal] = Field(
        description="Known cost per source: completion | translate | seo | media", examples=[{"completion": "0.0042"}]
    )
    cost_complete: bool = Field(description="False once a toolbox call reported no cost", examples=[True])
    profile_versions: list[int] = Field(description="Profile version ids its prompts used", examples=[[4, 9]])
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


class QaSpan(BaseModel):
    start: int = Field(description="Offset of the first character in the text", examples=[14])
    end: int = Field(description="Offset after the last character", examples=[18])
    text: str = Field(examples=["25%"])


class QaWarning(BaseModel):
    """A QA warning of a text value (contract A4 § 4)."""

    code: str = Field(description="rule | NUMBER_NOT_IN_FACTS | checklist", examples=["NUMBER_NOT_IN_FACTS"])
    message: str = Field(examples=["The number 25% is not in the facts registry or the source context."])
    span: QaSpan | None = Field(None, description="Where in the text; null when unknown")


class WarningsResponse(BaseModel):
    """`POST proposals/{id}/revise/` with `check_standard` — the proposal's warnings, no revision saved."""

    warnings: list[QaWarning]


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
    cost_usd: Decimal | None = Field(
        description="Share of the producing call's cost; null = unknown", examples=["0.0014"]
    )
    result_ref: str = Field(description="What applying it created (a document ref); blank otherwise", examples=[""])
    warnings: list[QaWarning] = Field(description="QA warnings of the current value")
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
    cost_usd: Decimal | None = Field(
        description="Cost of the completion behind it; null = unknown or none", examples=["0.0007"]
    )
    profile_versions: list[int] = Field(description="Profile version ids in its prompt", examples=[[4, 9]])
    warnings: list[QaWarning] = Field(description="QA warnings of this value")
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


class TemplateResponse(BaseModel):
    """`GET templates/` item — a saved worker run the CMS offers before "Custom…"."""

    key: str = Field(examples=["meta"])
    label: str = Field(examples=["Meta descriptions"])
    worker_key: str = Field(examples=["pim.writer"])
    worker_label: str = Field(examples=["Writer"])
    kind: str = Field(examples=["write"])
    target_module: str = Field(examples=["pim"])
    params: dict = Field(examples=[{"fields": ["meta_title", "meta_description"], "languages": ["*"]}])
    rules: list[str] = Field(examples=[["Brand name first."]])
    position: int = Field(examples=[10])
    active: bool


class FieldOptionResponse(BaseModel):
    value: str = Field(examples=["red"])
    label: str = Field(examples=["Red"])


class FieldResponse(BaseModel):
    """`GET fields/` item — one writable field of the adapter's catalog (contract A3 § 2)."""

    key: str = Field(examples=["meta_title"])
    label: str = Field(examples=["Meta title"])
    type: str = Field(description="text | html | select | multiselect", examples=["text"])
    max_length: int | None = Field(None, examples=[60])
    translatable: bool = True
    options: list[FieldOptionResponse] | None = None
    options_truncated: bool | None = None


class WorkflowStepResponse(BaseModel):
    """One step of a workflow definition."""

    key: str = Field(examples=["outline"])
    label: str = Field(examples=["Outline"])
    template: str = Field(examples=["article.outline"])
    needs: list[str] = Field(examples=[["keywords"]])
    gate: str = Field(description="review | auto", examples=["review"])
    output: str = Field(description="artifact | questions | document", examples=["artifact"])


class WorkflowDefinitionResponse(BaseModel):
    """`GET workflows/` item — an active workflow the CMS can start."""

    key: str = Field(examples=["article"])
    label: str = Field(examples=["Article"])
    target_module: str = Field(examples=["contentdb"])
    inputs_schema: dict = Field(description="JSON Schema of the brief", examples=[{"type": "object"}])
    steps: list[WorkflowStepResponse]


class RunStepResponse(WorkflowStepResponse):
    """A step of a run with its state; the output is the proposal `proposal_id` (`GET proposals/{id}/`)."""

    status: str = Field(description="waiting | running | review | approved | failed", examples=["review"])
    task_id: int | None = Field(examples=[42])
    proposal_id: int | None = Field(examples=[310])


class RunResponse(BaseModel):
    """A workflow run (`GET runs/{uid}/`, list item, start / regenerate / cancel result)."""

    uid: str = Field(examples=["3f2a9c0d4b6e4f1a8c7d2e5b9a0c1d3e"])
    definition: str = Field(examples=["article"])
    definition_label: str = Field(examples=["Article"])
    channel: str = Field(examples=["default"])
    inputs: dict = Field(examples=[{"topic": "How to care for a linen sofa"}])
    subject_ref: str = Field(description="What the final step created; blank before", examples=[""])
    status: str = Field(description="running | waiting | done | failed | cancelled", examples=["waiting"])
    current_step: str | None = Field(description="First step not approved yet", examples=["outline"])
    steps: list[RunStepResponse]
    cost_usd: Decimal | None = Field(description="Sum of the known costs of the run's tasks", examples=["0.0123"])
    cost_complete: bool = Field(description="False once one of its tasks has an unknown cost", examples=[True])
    created_by_id: int | None
    created_at: datetime
    modified_at: datetime


class RunListResponse(BaseModel):
    count: int
    next: str | None
    previous: str | None
    results: list[RunResponse]


class ProfileResponse(BaseModel):
    """`GET/PUT profile/?channel=` — one scope's own data (blank channel = the instance)."""

    model_config = ConfigDict(from_attributes=True)

    channel: str = Field(description="Blank = the whole instance, else a channel overlay", examples=[""])
    data: dict = Field(examples=[{"brand": {"text": "We build modular sofas.", "differentiators": ["Made to order"]}}])
    version: int = Field(description="0 = never saved", examples=[3])
    schema_version: int = Field(examples=[1])
    updated_by_id: int | None
    modified_at: datetime | None


class ProfileVersionResponse(BaseModel):
    """`GET profile/versions/` item — a snapshot of the data after one save."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int = Field(examples=[2])
    note: str = Field(examples=["restored version 1"])
    data: dict
    created_by_id: int | None
    created_at: datetime


class ProfileSectionStatus(BaseModel):
    key: str = Field(examples=["facts"])
    complete: bool = Field(examples=[False])
    minimum: str = Field(examples=["at least 3 verified facts"])


class ProfileStatusResponse(BaseModel):
    """`GET profile/status/?channel=` — completeness of the effective (merged) profile."""

    channel: str = Field(examples=["default"])
    complete: bool = Field(examples=[False])
    sections: list[ProfileSectionStatus]


class ProfileSchemaResponse(BaseModel):
    """`GET profile/schema/` — the JSON Schema `PUT profile/` validates against."""

    schema_version: int = Field(examples=[1])
    json_schema: dict = Field(description="JSON Schema (draft 2020-12) of `data`")


class MemoryEntryResponse(BaseModel):
    """`GET memory/` item, `PATCH memory/{id}/` result."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    channel: str = Field(examples=[""])
    subject_module: str = Field(examples=["pim"])
    subject_ref: str = Field(examples=["SKU-1"])
    kind: str = Field(description="seo_brief | answer | rule_suggestion | fact_suggestion", examples=["answer"])
    title: str = Field(examples=["Since when do you make sofas?"])
    text: str = Field(examples=["Since 1999."])
    data: dict
    source: str = Field(examples=["run:3f2a/ask"])
    status: str = Field(description="active | dismissed | promoted", examples=["active"])
    expires_at: datetime | None
    created_by_id: int | None
    created_at: datetime


class MemoryListResponse(BaseModel):
    count: int
    next: str | None
    previous: str | None
    results: list[MemoryEntryResponse]


class MemoryPromoteResponse(BaseModel):
    """`POST memory/{id}/promote/` — the promoted entry and the profile version it created."""

    entry: MemoryEntryResponse
    profile: ProfileResponse
