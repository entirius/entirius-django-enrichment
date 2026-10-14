# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Internal runner — executes a task with its Worker's kind, one subject at a time.

open → in_progress → done. Targets are pulled page by page through `task_service.resolve_targets`;
a target is a SKU string or a dict carrying `subject_ref`. A failing subject is logged with an
error id, counted in `counts.failed` and recorded in `task.errors` (stable code + error id); the task goes on. Every subject failed → `failed`.
The status is re-read before each subject: a task cancelled mid-run stops and stays `cancelled`.
POC: no retry, no beat, no budget logic.
"""

import logging
import uuid
from collections.abc import Iterator
from typing import Any

from django_enrichment.enums import TaskStatus, WorkerMode
from django_enrichment.kinds import Kind, ProposalDraft, extras, get_kind
from django_enrichment.models import EnrichmentTask, Worker
from django_enrichment.services import proposal_service, task_service, toolbox_calls, worker_service

_logger = logging.getLogger("process")


def run_task(task_id: int) -> None:
    task = task_service.get_task(task_id)
    worker = worker_service.find_active(task.type)
    if worker is None or worker.mode != WorkerMode.INTERNAL.value or task.status != TaskStatus.OPEN.value:
        _logger.info("runner skipped task %s (no active internal worker or not open)", task_id)
        return
    task_service.mark_in_progress(task)
    extras.clear_cache()
    try:
        succeeded, failed = _run_subjects(worker, task)
    except Exception as exc:  # noqa: BLE001 — resolve/kind setup failure must not leave the task in_progress
        _record_failure(task, "", exc)
        succeeded, failed = 0, 1
    finally:
        extras.clear_cache()
    status = TaskStatus.FAILED if failed and not succeeded else TaskStatus.DONE
    _finish(task, status.value)


def _finish(task: EnrichmentTask, status: str) -> None:
    """Close the task — unless someone else (an operator cancel) already moved it on."""
    try:
        task_service.transition_status(task, status)
    except ValueError:
        _logger.info("runner left task %s as %s (changed while running)", task.pk, task.status)


def _run_subjects(worker: Worker, task: EnrichmentTask) -> tuple[int, int]:
    kind = get_kind(worker.kind)()
    succeeded = failed = 0
    for subject_ref in _subjects(task):
        if not _still_in_progress(task):
            break
        if _run_subject(worker, kind, task, subject_ref):
            succeeded += 1
        else:
            failed += 1
    return succeeded, failed


def _run_subject(worker: Worker, kind: Kind, task: EnrichmentTask, subject_ref: str) -> bool:
    try:
        for draft in kind.run(worker, task, subject_ref):
            _intake(worker, task, subject_ref, draft)
    except Exception as exc:  # noqa: BLE001 — per-subject isolation: one failing subject must not stop the task
        _record_failure(task, subject_ref, exc)
        task_service.increment_count(task, "failed")
        return False
    return True


def _record_failure(task: EnrichmentTask, subject_ref: str, exc: Exception) -> None:
    """Log the trace under an error id; the task keeps only the id and a stable code (no exception text)."""
    error_id = _error_id()
    _logger.exception("runner failed on subject %r of task %s [%s]", subject_ref, task.pk, error_id)
    code = toolbox_calls.failure_code(exc)
    task_service.record_error(task, subject_ref=subject_ref, code=code, error_id=error_id)


def _intake(worker: Worker, task: EnrichmentTask, subject_ref: str, draft: ProposalDraft) -> None:
    """Hand one draft to intake — `intake_media` (staged binary) when it carries a media file."""
    fields = {
        "target_module": worker.target_module,
        "subject_ref": subject_ref,
        "target_kind": draft.target_kind,
        "target_locator": draft.target_locator,
        "proposed_value": draft.proposed_value,
        "task": task,
        "subject_label": draft.subject_label,
        "source": f"worker:{worker.key}@v{worker.version}",
    }
    if draft.media_file is None:
        proposal_service.intake(**fields)
    else:
        proposal_service.intake_media(file=draft.media_file, **fields)


def _subjects(task: EnrichmentTask) -> Iterator[str]:
    """Distinct subject refs, page by page. Gap mode is a work queue (always page 1); the seen-set
    ends the loop when a page brings nothing new (failed gap subjects stay in the queue)."""
    seen: set[str] = set()
    page = 1
    while fresh := _fresh_refs(task_service.resolve_targets(task, page=page), seen):
        seen.update(fresh)
        yield from fresh
        if task.scope_spec.get("mode") != "gap":
            page += 1


def _fresh_refs(targets: list[Any], seen: set[str]) -> list[str]:
    refs = [target["subject_ref"] if isinstance(target, dict) else str(target) for target in targets]
    return list(dict.fromkeys(ref for ref in refs if ref not in seen))


def _still_in_progress(task: EnrichmentTask) -> bool:
    """Re-read the status before each subject — a cancelled task stops spending toolbox calls."""
    task.refresh_from_db(fields=["status"])
    return task.status == TaskStatus.IN_PROGRESS.value


def _error_id() -> str:
    return uuid.uuid4().hex[:12]
