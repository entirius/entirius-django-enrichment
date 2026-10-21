# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Augmentations (contract A4 § 5) — deterministic research steps that add data blocks to a kind's prompt.

`register_augmentation(key, fn)`; `fn(*, task, subject_ref, context) -> AugmentResult`. A task enables them with
`params.augment: [keys]` (validated against the registry at spawn; templates may preset it). The model never
calls a tool itself: the augmentation runs before the completion and its result is a fenced data block. Its
toolbox calls charge the task through `toolbox_calls` (cost ledger), `AugmentResult.cost` is their total for the
caller's information only; `memory_writes` are saved by `run_all`.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from django_enrichment.kinds.base import SubjectError, fenced
from django_enrichment.models import MemoryEntry
from django_enrichment.services import memory_service


@dataclass
class AugmentResult:
    block: str
    cost: Decimal | None = None
    memory_writes: list[MemoryEntry] = field(default_factory=list)


Augmentation = Callable[..., AugmentResult]
_REGISTRY: dict[str, Augmentation] = {}


def register_augmentation(key: str, fn: Augmentation) -> None:
    _REGISTRY[key] = fn


def keys() -> list[str]:
    return sorted(_REGISTRY)


class AugmentParams(BaseModel):
    """The task params augmentations read; kept next to the kind's params at spawn."""

    augment: list[str] = Field(default_factory=list, description="Augmentation keys", examples=[["seo_brief"]])
    seo_scope: Literal["subject", "category"] | None = Field(None, description="Where seo_brief is remembered")
    seo_detail: Literal["brief", "full"] | None = Field(
        None, description="`full` adds the search evidence (volumes, SERP, people also ask) to the seo_brief block"
    )

    @field_validator("augment")
    @classmethod
    def _registered(cls, value: list[str]) -> list[str]:
        if unknown := sorted(set(value) - set(_REGISTRY)):
            raise ValueError(f"unknown augmentations {unknown} (known: {keys()})")
        return list(dict.fromkeys(value))


def carried_params(params: dict) -> dict:
    """The augmentation keys of `params`, validated (only those given). Raises `ValueError`."""
    try:
        return AugmentParams.model_validate(params).model_dump(exclude_unset=True)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"params invalid: {details}") from exc


def enabled(task: Any) -> list[str]:
    return list((getattr(task, "params", None) or {}).get("augment") or [])


def run_all(*, task: Any, subject_ref: str, context: dict) -> list[str]:
    """The task's augmentations in order, as fenced data blocks (blank ones dropped); saves their memory writes."""
    blocks = [_run(key, task=task, subject_ref=subject_ref, context=context) for key in enabled(task)]
    return [block for block in blocks if block]


def _run(key: str, *, task: Any, subject_ref: str, context: dict) -> str:
    if (fn := _REGISTRY.get(key)) is None:
        raise SubjectError("AUGMENT_UNKNOWN", f"augmentation {key!r} is not registered")
    result = fn(task=task, subject_ref=subject_ref, context=context)
    for entry in result.memory_writes:
        memory_service.remember(entry)
    return fenced(key, result.block) if result.block else ""
