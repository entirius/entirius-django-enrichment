# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Optional context seams — company profile (contract A4 § 2), memory (§ 3), augmentations (§ 5).

Each seam is a callable looked up when it is used; until the module that provides it exists, the seam
answers "nothing" and the prompt simply has no such section. Expected callables:

| seam | callable |
|---|---|
| profile | `services.profile_service.context_for(*, channel, task_type, kind) -> (instructions, data)` |
| memory recall | `services.memory_service.recall(*, channel, subject_module, subject_ref, kinds, budget_chars) -> str` |
| answers written | `services.memory_service.record_answers(*, run, step, value) -> None` |
| augmentations | `augmentations.run_all(*, task, subject_ref, context: dict) -> list[str]` (data blocks) |
"""

import importlib
from collections.abc import Callable
from typing import Any

PROFILE_SERVICE = "django_enrichment.services.profile_service"
MEMORY_SERVICE = "django_enrichment.services.memory_service"
AUGMENTATIONS = "django_enrichment.augmentations"
MEMORY_BUDGET_CHARS = 3000


def _seam(module_path: str, name: str) -> Callable[..., Any] | None:
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name != module_path:
            raise
        return None
    return getattr(module, name, None)


def profile(*, channel: str, task_type: str, kind: str) -> tuple[str, str]:
    """`(instructions, data)` of the company profile for this task; blank without a profile service."""
    context_for = _seam(PROFILE_SERVICE, "context_for")
    return context_for(channel=channel, task_type=task_type, kind=kind) if context_for else ("", "")


def recall(*, channel: str, subject_module: str, subject_ref: str, kinds: list[str]) -> str:
    """Remembered entries as a data block; blank without a memory service."""
    recall_fn = _seam(MEMORY_SERVICE, "recall")
    if recall_fn is None:
        return ""
    return recall_fn(
        channel=channel,
        subject_module=subject_module,
        subject_ref=subject_ref,
        kinds=kinds,
        budget_chars=MEMORY_BUDGET_CHARS,
    )


def augmentation_blocks(*, task: Any, subject_ref: str, context: dict) -> list[str]:
    """Data blocks of the task's augmentations (`params.augment`); none without the registry."""
    run_all = _seam(AUGMENTATIONS, "run_all")
    return run_all(task=task, subject_ref=subject_ref, context=context) if run_all else []


def record_answers(*, run: Any, step: str, value: dict) -> None:
    """Remember approved answers (memory `answer`); a no-op without a memory service."""
    if record := _seam(MEMORY_SERVICE, "record_answers"):
        record(run=run, step=step, value=value)
