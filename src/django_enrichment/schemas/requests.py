# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Pydantic request schemas for the enrichment admin API (etap-05).

Validation happens at the API boundary; the service layer owns business rules (scope_spec
validity, state-machine edges). These schemas keep the wire contract explicit and feed the
OpenAPI schema via drf-spectacular.
"""

import json
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class TaskSpawnRequest(BaseModel):
    """`POST /tasks/` — spawn a typed work order. `scope_spec` is opaque to the bus but the
    service validates `mode` + `module` (the adapter key)."""

    type: str = Field(min_length=1, max_length=64, examples=["fix-attribute"])
    scope_spec: dict = Field(examples=[{"mode": "list", "module": "pim", "channel": "default", "refs": ["SKU-1"]}])
    params: dict = Field(default_factory=dict, examples=[{"feature": "description"}])
    batch_key: str = Field("", max_length=128, description="Analyzer coalescing key (find-or-append)")


class TaskCSVImportRequest(BaseModel):
    """`POST /tasks/import-csv/` — multipart CSV import (etap-11).

    The CSV arrives as the multipart `file` part (handled in the view); these are the metadata form
    fields. One file = one channel + one language. The task work-type is NOT here — it comes from the
    CSV's `type` column (rows grouped by it → N tasks). In multipart `params` arrives as a string, so
    a `before` validator parses it (mirrors `ProposalMediaIntakeRequest`)."""

    module: str = Field("pim", min_length=1, max_length=32, examples=["pim"])
    channel: str = Field(min_length=1, max_length=128, examples=["default"])
    language: str = Field(min_length=1, max_length=8, examples=["pl"])
    params: dict = Field(default_factory=dict)

    @field_validator("params", mode="before")
    @classmethod
    def _parse_json(cls, v):
        if isinstance(v, str):
            if not v:
                return {}
            try:
                return json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(f"params must be valid JSON: {exc}") from exc
        return v


class TaskUpdateRequest(BaseModel):
    """`PATCH /tasks/{id}/` — drive the task state machine (e.g. in_progress, done, cancelled)."""

    status: str = Field(examples=["in_progress", "done"])


# DoS guard on the opaque proposal body: a single product field (description, SEO copy) is far
# smaller than this. JSONField itself is unbounded, so the cap lives here at the API boundary.
_MAX_PROPOSED_VALUE_BYTES = 65536  # 64 KiB


class ProposalIntakeRequest(BaseModel):
    """`POST /proposals/` — a worker submits one proposal. Idempotent on `external_ref`."""

    target_module: str = Field(min_length=1, max_length=32, examples=["pim"])
    subject_ref: str = Field(min_length=1, max_length=255, examples=["SKU-1"])
    target_kind: str = Field(min_length=1, max_length=64, examples=["attribute_value"])
    target_locator: dict = Field(examples=[{"channel": "default", "language": "pl", "feature_idx": "description"}])
    proposed_value: dict = Field(examples=[{"text": "A better description."}])
    task_id: int | None = Field(None, description="Owning task; links progress counts")
    target_type: str = Field("", max_length=64)
    subject_label: str = Field("", max_length=512, description="Denormalised label for the review queue")
    subject_url: str = Field("", max_length=2048, description="Denormalised deep-link to the source editor")
    source: str = Field("", max_length=128, examples=["n8n:describe-v2"])
    confidence: Decimal | None = Field(None, ge=0, le=1, examples=["0.900"])
    batch_id: str = Field("", max_length=128)
    external_ref: str = Field("", max_length=255, description="Worker execution id — intake idempotency key")
    # `staged_file` is intentionally NOT accepted here — media goes through `upload-media/`, which
    # stages the binary itself. A client-settable ref would let a text proposal point at an arbitrary
    # staged file (security review etap-08).

    @field_validator("proposed_value")
    @classmethod
    def _cap_proposed_value_size(cls, v: dict) -> dict:
        if len(json.dumps(v, ensure_ascii=False).encode("utf-8")) > _MAX_PROPOSED_VALUE_BYTES:
            raise ValueError(f"proposed_value exceeds the {_MAX_PROPOSED_VALUE_BYTES}-byte limit")
        return v


class ProposalMediaIntakeRequest(BaseModel):
    """`POST /proposals/upload-media/` — multipart media intake (etap-08).

    The binary arrives as the multipart `file` part (handled in the view); these are the metadata
    form fields. `target_kind` is fixed to `"picture"` by the service. In multipart the JSON-typed
    fields (`target_locator`, `proposed_value`) arrive as strings, so a `before` validator parses
    them. `proposed_value` carries `{"op": "replace_main", "alt_t9n": {...}}` — `alt_t9n` optional.
    """

    target_module: str = Field("pim", min_length=1, max_length=32, examples=["pim"])
    subject_ref: str = Field(min_length=1, max_length=255, examples=["SKU-1"])
    target_locator: dict = Field(examples=[{"channel": "default"}])
    proposed_value: dict = Field(default_factory=lambda: {"op": "replace_main"})
    task_id: int | None = None
    target_type: str = Field("", max_length=64)
    subject_label: str = Field("", max_length=512)
    subject_url: str = Field("", max_length=2048)
    source: str = Field("", max_length=128, examples=["n8n:comfyui-v1"])
    confidence: Decimal | None = Field(None, ge=0, le=1)
    batch_id: str = Field("", max_length=128)
    external_ref: str = Field("", max_length=255)

    @field_validator("target_locator", "proposed_value", mode="before")
    @classmethod
    def _parse_json(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(f"must be valid JSON: {exc}") from exc
        return v


class RejectRequest(BaseModel):
    """`POST /proposals/{id}/reject/`."""

    reason: str = Field("", max_length=512)


class BulkAcceptRequest(BaseModel):
    """`POST /proposals/bulk-accept/` — filters select the PENDING set to apply (async).

    Field names mirror the `GET /proposals/` query params so a client can copy filters verbatim;
    `to_filters` maps `target_module` → the service's `module` kwarg.
    """

    target_module: str | None = None
    target_kind: str | None = None
    batch_id: str | None = None
    source: str | None = None
    confidence_min: Decimal | None = Field(None, ge=0, le=1)
    search: str | None = None

    def _filters(self, *, exclude: set[str]) -> dict:
        data = self.model_dump(exclude_none=True, exclude=exclude)
        if "target_module" in data:
            data["module"] = data.pop("target_module")  # list_for_review kwarg name
        return data

    def to_filters(self) -> dict:
        """Filters for `list_for_review` — unset keys dropped so service defaults apply."""
        return self._filters(exclude=set())


class BulkRejectRequest(BulkAcceptRequest):
    """`POST /proposals/bulk-reject/` — same filter set plus a reason."""

    reason: str = Field("", max_length=512)

    def to_filters(self) -> dict:
        return self._filters(exclude={"reason"})


class BulkUndoRequest(BulkAcceptRequest):
    """`POST /proposals/bulk-undo/` — the filter set selecting the APPLIED proposals to revert (etap-09).

    Same fields as bulk-accept so a client copies filters verbatim; the service scopes to `applied`.
    """


class SpawnRuleCreateRequest(BaseModel):
    """`POST /spawn-rules/` — operator config mapping a module quality check to a work-type
    (etap-13). `check_key` is NOT validated against the module here (decoupling) — an unknown key
    fails loud at resolve time, inside the adapter."""

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$", examples=["desc-pl"])
    module: str = Field("pim", min_length=1, max_length=32, examples=["pim"])
    check_key: str = Field(min_length=1, max_length=64, examples=["pl-description"])
    params: dict = Field(default_factory=dict, description="Forwarded to find_gaps (reserved)")
    scope: dict = Field(default_factory=dict, examples=[{"channel": "default", "language": "pl"}])
    task_type: str = Field(min_length=1, max_length=64, examples=["fix-attribute"])
    task_params: dict = Field(default_factory=dict, examples=[{"feature": "description"}])
    limit: int | None = Field(None, ge=1, description="Max targets per task (null = module default)")
    cooldown_days: int | None = Field(None, ge=0, description="Rejected/applied cooldown (null = module default)")
    auto: bool = Field(False, description="Spawn automatically via the gap_autospawn beat")
    active: bool = True


class SpawnRuleUpdateRequest(BaseModel):
    """`PATCH /spawn-rules/{key}/` — all fields optional; `key` is immutable (not present here)."""

    module: str | None = Field(None, min_length=1, max_length=32)
    check_key: str | None = Field(None, min_length=1, max_length=64)
    params: dict | None = None
    scope: dict | None = None
    task_type: str | None = Field(None, min_length=1, max_length=64)
    task_params: dict | None = None
    limit: int | None = Field(None, ge=1)
    cooldown_days: int | None = Field(None, ge=0)
    auto: bool | None = None
    active: bool | None = None
