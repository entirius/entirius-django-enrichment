# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Spawn of the `comfy` workers (`enrichment.comfy_spawn` beat): which main pictures are still to be checked.

For each active internal `comfy` worker that is not paused and has no task open or in progress: when ComfyUI answers,
page the adapter's `main_pictures(channel, page)` and take up to `ENRICHMENT_COMFY_SPAWN_LIMIT` products whose main
picture's sha1 has no `ComfyResult` under the current workflow file and worker version, then spawn one `list` task on
the bulk lane. The pages are walked from where the last spawn stopped (a cursor in the cache) and wrap around, so
pictures that fail every time do not hold the rest back. The host's beat schedule is the time window.
"""

import logging

from django.conf import settings
from django.core.cache import cache

from django_enrichment.adapters import get_adapter
from django_enrichment.enums import TaskLane, TaskStatus, WorkerMode
from django_enrichment.kinds.comfy import ComfyOptions
from django_enrichment.models import EnrichmentTask, Worker
from django_enrichment.services import comfy_result_service, comfyui_client, task_service

_logger = logging.getLogger("process")
KIND = "comfy"
_DEFAULT_LIMIT = 100
_CURSOR_KEY = "enrichment:comfy_spawn:{key}:page"
_CURSOR_TTL_S = 7 * 24 * 3600
_RUNNING = (TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value)


def spawn_all() -> dict[str, list[str]]:
    """One spawn per worker; a worker that fails is logged and skipped, never stops the others."""
    summary: dict[str, list[str]] = {"spawned": [], "skipped": [], "failed": []}
    workers = Worker.objects.filter(kind=KIND, active=True, mode=WorkerMode.INTERNAL.value, paused_reason="")
    for worker in workers.order_by("key"):
        try:
            outcome = "spawned" if spawn(worker) is not None else "skipped"
        except Exception:  # noqa: BLE001 — one broken worker (a missing workflow file) must not stop the others
            _logger.exception("comfy_spawn: worker %s failed", worker.key)
            outcome = "failed"
        summary[outcome].append(worker.key)
    return summary


def spawn(worker: Worker) -> EnrichmentTask | None:
    """The worker's next task, or None: one still runs, ComfyUI does not answer, or nothing is left to check."""
    if EnrichmentTask.objects.filter(type=worker.key, status__in=_RUNNING).exists():
        return None
    if not comfyui_client.available():
        _logger.info("comfy_spawn: ComfyUI not answering, %s waits for the next beat", worker.key)
        return None
    refs = unchecked_refs(worker, limit=getattr(settings, "ENRICHMENT_COMFY_SPAWN_LIMIT", _DEFAULT_LIMIT))
    if not refs:
        return None
    scope = {"mode": "list", "module": worker.target_module, "refs": refs}
    return task_service.spawn(type=worker.key, scope_spec=scope, params={}, lane=TaskLane.QUEUE.value)


def unchecked_refs(worker: Worker, *, limit: int) -> list[str]:
    """Up to `limit` products whose main picture is still to be checked, from the cursor page on, wrapping once."""
    options = ComfyOptions.model_validate(worker.options)
    _, workflow_hash = comfy_result_service.load_workflow(options.workflow)
    listing = getattr(get_adapter(worker.target_module), "main_pictures", None)
    if listing is None:
        raise ValueError(f"adapter {worker.target_module!r} has no main_pictures")
    start = cache.get(_CURSOR_KEY.format(key=worker.key)) or 1
    refs: list[str] = []
    page, wrapped = start, False
    while len(refs) < limit and not (wrapped and page >= start):
        if not (rows := listing(channel=options.channel, page=page)):
            if page == 1 or wrapped:
                break
            page, wrapped = 1, True
            continue
        due, room = _unchecked(worker, workflow_hash, rows), limit - len(refs)
        refs += due[:room]
        if len(due) > room:
            break  # the rest of this page is next spawn's start
        page += 1
    cache.set(_CURSOR_KEY.format(key=worker.key), page, timeout=_CURSOR_TTL_S)
    return refs


def _unchecked(worker: Worker, workflow_hash: str, rows: list[dict]) -> list[str]:
    """The rows' products whose picture has no result yet. A picture without a sha1 cannot be matched, so it is
    left out (it would be checked on every spawn)."""
    sha1s = {row["sha1"] for row in rows if row.get("sha1")}
    checked = comfy_result_service.checked(
        worker_key=worker.key, workflow_hash=workflow_hash, worker_version=worker.version, sha1s=sha1s
    )
    return [row["subject_ref"] for row in rows if row.get("sha1") and (row["subject_ref"], row["sha1"]) not in checked]
