# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Worker service — lookup, params validation, versioned save, internal-runner dispatch.

A `Worker` row configures the executor of one task type (`key == task.type`). When an active
internal Worker exists, a freshly spawned task is handed to the runner after commit; external
workers (n8n) keep pulling through the API exactly as before.
"""

import re
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from pydantic import ValidationError as PydanticValidationError

from django_enrichment.enums import WorkerMode
from django_enrichment.kinds import get_kind
from django_enrichment.models import EnrichmentTask, Worker
from django_enrichment.models.worker import VERSIONED_FIELDS
from django_enrichment.services import lane_service

_SOURCE_PATTERN = re.compile(r"^worker:(?P<key>[a-z0-9][a-z0-9_.-]*)@v\d+$")


def from_source(source: str) -> Worker | None:
    """The Worker that produced a proposal (`source` = `worker:<key>@v<n>`), active or not; else None."""
    if not (match := _SOURCE_PATTERN.match(source)):
        return None
    return Worker.objects.filter(key=match["key"]).first()


def find_active(key: str) -> Worker | None:
    return Worker.objects.filter(key=key, active=True).first()


def list_active(target_module: str | None = None) -> QuerySet[Worker]:
    qs = Worker.objects.filter(active=True)
    if target_module is not None:
        qs = qs.filter(target_module=target_module)
    return qs.order_by("key")


def describe(worker: Worker) -> dict[str, Any]:
    """The CMS view of a worker: identity plus the JSON schema of the params a task must carry."""
    return {
        "key": worker.key,
        "label": worker.label,
        "kind": worker.kind,
        "target_module": worker.target_module,
        "mode": worker.mode,
        "params_schema": get_kind(worker.kind).Params.model_json_schema(),
    }


def validate_params(task_type: str, params: dict) -> dict:
    """Validate task params against the kind of the active Worker for `task_type` (none → unchanged).

    Returns the normalised params (defaults filled). Raises `ValueError` with field-level messages.
    """
    worker = find_active(task_type)
    if worker is None:
        return params
    try:
        return get_kind(worker.kind).Params.model_validate(params).model_dump()
    except PydanticValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'params'}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"params invalid for worker {task_type!r}: {details}") from exc


def dispatch_if_internal(task: EnrichmentTask) -> None:
    """Queue the internal runner for `task` on its lane's Celery queue once the spawning transaction commits."""
    worker = find_active(task.type)
    if worker is None or worker.mode != WorkerMode.INTERNAL.value:
        return
    from django_enrichment.tasks.worker_runner import run_task

    queue = lane_service.celery_queue(task.lane)
    transaction.on_commit(lambda: run_task.apply_async(args=[task.pk], queue=queue))


def save_worker(worker: Worker) -> Worker:
    """Validate (`full_clean` → kind `Options`), bump `version` on a calibration change, save."""
    worker.full_clean()
    _bump_version(worker)
    worker.save()
    return worker


def _bump_version(worker: Worker) -> None:
    if worker.pk is None:
        return
    previous = Worker.objects.filter(pk=worker.pk).values("version", *VERSIONED_FIELDS).first()
    if previous and any(previous[name] != getattr(worker, name) for name in VERSIONED_FIELDS):
        worker.version = previous["version"] + 1
