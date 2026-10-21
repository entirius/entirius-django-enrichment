# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Task service — spawn / pull / resolve targets / progress (framework-agnostic).

`EnrichmentTask` is a thin work order: one row per command, targets resolved lazily through
the adapter (the table grows with commands, not the catalogue). The state machine lives here,
not on the model (`TASK_STATUS_TRANSITIONS` declares the edges; this layer enforces them).

`scope_spec` shape (opaque JSON on the model, interpreted here):
    {"mode": "list",   "module": "pim", "refs": ["SKU-1", ...]}
    {"mode": "filter", "module": "pim", ...module-side filter keys...}
    {"mode": "csv",    "module": "pim", "file": "<staging ref>"}        # streamed in etap-11
    {"mode": "gap",    "module": "pim", "check": "missing_feature", "params": {}, "scope": {}}

`module` keys the adapter registry — `EnrichmentTask` has no `target_module` column (model frozen
in etap-02), so the bus reads it from `scope_spec`. `filter` is a simplified `gap` (no named
check); `gap` routes to `adapter.find_gaps`, the rest to `adapter.resolve_targets`.
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Count, QuerySet
from django.utils import timezone

from django_enrichment import augmentations
from django_enrichment.adapters import get_adapter
from django_enrichment.enums import TASK_STATUS_TRANSITIONS, ProposalStatus, TaskStatus
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import csv_import, lane_service, qa_auto_check, template_service, worker_service

VALID_SCOPE_MODES = frozenset({"filter", "csv", "list", "gap"})
ERRORS_CAP = 20
RETRY_OF = "retry_of"


def _validate_scope_spec(scope_spec: dict) -> None:
    mode = scope_spec.get("mode")
    if mode not in VALID_SCOPE_MODES:
        raise ValueError(f"scope_spec.mode must be one of {sorted(VALID_SCOPE_MODES)}, got {mode!r}")
    if not scope_spec.get("module"):
        raise ValueError("scope_spec.module is required (keys the adapter registry, e.g. 'pim')")
    if mode == "csv" and not scope_spec.get("file"):
        raise ValueError("scope_spec.file is required for mode 'csv' (the staged CSV ref)")


def _append_refs(task: EnrichmentTask, new_refs: list[str]) -> EnrichmentTask:
    """Find-or-append for the analyzer: extend a list-mode task's refs, deduped, order kept."""
    merged = list(dict.fromkeys([*task.scope_spec.get("refs", []), *new_refs]))
    task.scope_spec["refs"] = merged
    task.save(update_fields=["scope_spec", "modified_at"])
    worker_service.dispatch(task)  # plans the new refs into units (a no-op when none are new)
    return task


def resolve_type_and_params(task_type: str, params: dict, template: str) -> tuple[str, dict]:
    """Validated `(type, params)`; `params.augment` / `seo_scope` / `qa_check` and a template's key survive kind
    validation."""
    if template:
        task_type, params = template_service.merge_into_task(template, task_type=task_type, params=params)
    if not task_type:
        raise ValueError("give a task type or a template")
    carried = {**augmentations.carried_params(params), **qa_auto_check.carried_params(params)}
    params = {**worker_service.validate_params(task_type, params), **carried}
    return task_type, {**params, "template": template} if template else params


def spawn(
    *,
    type: str,
    scope_spec: dict,
    params: dict | None = None,
    requested_by: AbstractBaseUser | None = None,
    batch_key: str = "",
    template: str = "",
    lane: str = "",
    retry_of: int | None = None,
) -> EnrichmentTask:
    """Create a task, or coalesce into an open one sharing `batch_key` (analyzer rolling task).

    When an active Worker owns `type`, `params` are validated by its kind and a new task is handed
    to the internal runner after commit (`worker_service.dispatch`). With `template`, the
    template decides `type` and presets params/rules (`template_service.merge_into_task`). `lane` is
    `now` | `queue` | blank = by size (`lane_service.resolve`; `now` over the cap → `ValueError`). `retry_of` is the
    id of the task this one retries (`retry_service`); it lands in `params.retry_of`, which kind validation would drop.
    """
    _validate_scope_spec(scope_spec)
    type, params = resolve_type_and_params(type, params or {}, template)
    if retry_of is not None:
        params = {**params, RETRY_OF: retry_of}
    lane = lane_service.resolve(lane, scope_spec)

    if batch_key:
        open_task = EnrichmentTask.objects.filter(batch_key=batch_key, status=TaskStatus.OPEN.value).first()
        if open_task is not None:
            if scope_spec.get("mode") == "list":
                return _append_refs(open_task, list(scope_spec.get("refs", [])))
            return open_task

    task = EnrichmentTask.objects.create(
        type=type, scope_spec=scope_spec, params=params, requested_by=requested_by, batch_key=batch_key, lane=lane
    )
    worker_service.dispatch(task)
    return task


def import_csv(
    *,
    file,
    module: str,
    channel: str,
    language: str,
    params: dict | None = None,
    requested_by: AbstractBaseUser | None = None,
) -> list[EnrichmentTask]:
    """Spawn N single-type tasks from one uploaded CSV (`sku,field,type`), grouped by work-type.

    One file = one channel + one language (from the upload dialog). The file is staged once; rows are
    grouped by their `type` column (the work-type → `task.type`), so a file mixing `fix-attribute`
    and `translate` produces two tasks, each streaming only its own rows at resolve time. Raises
    `ValueError` (via `csv_import.validate_and_stage`) on a malformed file — nothing is spawned then.
    """
    ref, types = csv_import.validate_and_stage(file)
    tasks = [
        spawn(
            type=work_type,
            scope_spec={
                "mode": "csv",
                "module": module,
                "file": ref,
                "channel": channel,
                "language": language,
                "row_type": work_type,
            },
            params=params,
            requested_by=requested_by,
        )
        for work_type in types
    ]
    return tasks


def list_open(type: str | None = None) -> QuerySet[EnrichmentTask]:
    """Open tasks for the worker to pull, oldest first."""
    qs = EnrichmentTask.objects.filter(status=TaskStatus.OPEN.value)
    if type is not None:
        qs = qs.filter(type=type)
    return qs.order_by("created_at")


@dataclass(frozen=True)
class TaskFilters:
    """Admin task-list filters; `None` = no filter. `statuses` match any; `subject_ref` needs `refs` (list mode)."""

    type: str | None = None
    statuses: list[str] | None = None
    batch_key: str | None = None
    subject_ref: str | None = None
    target_module: str | None = None


def list_tasks(filters: TaskFilters | None = None) -> QuerySet[EnrichmentTask]:
    """Filtered task list for the admin API (any status, newest first)."""
    f = filters or TaskFilters()
    qs = EnrichmentTask.objects.all()
    if f.type is not None:
        qs = qs.filter(type=f.type)
    if f.statuses:
        qs = qs.filter(status__in=f.statuses)
    if f.batch_key is not None:
        qs = qs.filter(batch_key=f.batch_key)
    if f.target_module is not None:
        qs = qs.filter(scope_spec__module=f.target_module)
    if f.subject_ref:
        qs = qs.filter(scope_spec__refs__icontains=_json_token(f.subject_ref))
    return qs.order_by("-created_at")


def _json_token(ref: str) -> str:
    """`"<ref>"` as it is serialised inside the `refs` JSON array — the quotes make the match whole-element.

    A text match on the array, not JSON containment: `contains` on a JSON key is Postgres-only and the suite
    runs on SQLite. Case-insensitive (`icontains`); SKUs differing only in case are not a real catalogue.
    """
    return json.dumps(ref, ensure_ascii=False)


def get_task(task_id: int) -> EnrichmentTask:
    """Fetch a task by pk. Raises `ValueError(... not found)` so the API maps it to 404 without
    importing the model into the view layer."""
    try:
        return EnrichmentTask.objects.get(pk=task_id)
    except EnrichmentTask.DoesNotExist as exc:
        raise ValueError(f"Task {task_id} not found") from exc


def resolve_targets(task: EnrichmentTask, page: int = 1) -> list[Any]:
    """Resolve the task's scope into targets via its adapter (lazy/paged — never materialises all)."""
    scope_spec = task.scope_spec
    _validate_scope_spec(scope_spec)
    if scope_spec["mode"] == "csv":
        # The CSV lives in the bus's own staging store — a source adapter must never read it
        # (decision D1). `sku,field,type` is generic, so the bus streams targets itself and never
        # hands the staged ref to `adapter.resolve_targets`.
        return csv_import.stream_targets(scope_spec["file"], work_type=scope_spec["row_type"], page=page)
    adapter = get_adapter(scope_spec["module"])
    if scope_spec["mode"] == "gap":
        return resolve_gap_page(scope_spec, task.params, task.counts, page)
    return list(adapter.resolve_targets(scope_spec, page))


def resolve_gap_page(scope_spec: dict, params: dict, counts: dict, page: int = 1) -> list[Any]:
    """Gap-mode resolve: ``adapter.find_gaps`` + proposal dedup/cooldown filter + limit budget.

    WORK-QUEUE CONTRACT: a gap-mode worker always re-pulls ``page=1`` until it comes back empty —
    submitted (pending) proposals make consumed targets drop off the front, so sequential paging
    would silently skip items. The ``page`` arg exists for inspection, not for ``page++`` loops.

    `find_gaps`' signature (check, params, scope) is fixed by the cross-module contract, so paging
    rides in ``scope["page"]`` — the adapter MUST bound its result to that page, never return the
    whole catalogue (1M rows).

    ``params`` (the task's) may carry ``limit`` (max targets this task feeds the worker — once
    ``counts["proposed"]`` reaches it, the queue reads empty and the worker marks the task done)
    and ``cooldown_days`` (see ``_filter_gap_candidates``).
    """
    limit = params.get("limit")
    proposed = counts.get("proposed", 0)
    if limit is not None and proposed >= limit:
        return []

    adapter = get_adapter(scope_spec["module"])
    scope = {**scope_spec.get("scope", {}), "page": page}
    candidates = list(adapter.find_gaps(scope_spec["check"], scope_spec.get("params", {}), scope))
    candidates = _filter_gap_candidates(candidates, target_module=scope_spec["module"], params=params)
    if limit is not None:
        candidates = candidates[: limit - proposed]
    return candidates


def _spawn_cooldown_days() -> int:
    return getattr(settings, "ENRICHMENT_SPAWN_COOLDOWN_DAYS", 7)


def _filter_gap_candidates(candidates: list[Any], *, target_module: str, params: dict) -> list[Any]:
    """Drop candidates whose target already has an answer in flight or in cooldown.

    Dedup key is the coarse generic pair ``(subject_ref, target_kind)`` — deliberately NOT
    ``locator_hash``: the bus never reads the opaque ``target_locator``, and one in-flight
    proposal per (product, kind) is the wanted behaviour for gap tasks. Blocking table:

    - ``pending`` / ``drifted``  → always (an answer is sitting in the review queue; drifted
      awaits the per-item re-confirm, D2)
    - ``rejected``               → for ``cooldown_days`` from ``reviewed_at`` (breaks the
      reject → respawn → regenerate loop)
    - ``applied``                → for ``cooldown_days`` from ``applied_at`` (a write that still
      fails the check must not burn LLM calls every pull)
    - ``superseded`` / ``reverted`` → never block

    Non-dict candidates (the contract allows opaque items; the test fake returns strings) pass
    through untouched.
    """
    refs = {c["subject_ref"] for c in candidates if isinstance(c, dict) and c.get("subject_ref")}
    if not refs:
        return candidates

    cooldown = timedelta(days=params.get("cooldown_days", _spawn_cooldown_days()))
    now = timezone.now()
    blocked: set[tuple[str, str]] = set()
    rows = ContentProposal.objects.filter(
        target_module=target_module,
        subject_ref__in=refs,
        status__in=(
            ProposalStatus.PENDING.value,
            ProposalStatus.DRIFTED.value,
            ProposalStatus.REJECTED.value,
            ProposalStatus.APPLIED.value,
        ),
    ).values_list("subject_ref", "target_kind", "status", "reviewed_at", "applied_at")
    for subject_ref, target_kind, prop_status, reviewed_at, applied_at in rows:
        if prop_status in (ProposalStatus.PENDING.value, ProposalStatus.DRIFTED.value):
            blocked.add((subject_ref, target_kind))
        elif prop_status == ProposalStatus.REJECTED.value:
            if reviewed_at is not None and now - reviewed_at < cooldown:
                blocked.add((subject_ref, target_kind))
        elif applied_at is not None and now - applied_at < cooldown:
            blocked.add((subject_ref, target_kind))

    return [
        c for c in candidates if not isinstance(c, dict) or (c.get("subject_ref"), c.get("target_kind")) not in blocked
    ]


def transition_status(
    task: EnrichmentTask,
    new_status: str,
    *,
    user: AbstractBaseUser | None = None,  # noqa: ARG001 — API symmetry
) -> EnrichmentTask:
    allowed = TASK_STATUS_TRANSITIONS.get(task.status)
    if allowed is None or new_status not in allowed:
        raise ValueError(f"invalid task transition: {task.status} -> {new_status}")
    now = timezone.now()
    # Conditional on the status we read: a stale object must not overwrite a concurrent change.
    updated = EnrichmentTask.objects.filter(pk=task.pk, status=task.status).update(status=new_status, modified_at=now)
    if not updated:
        raise ValueError(f"task {task.pk} is no longer {task.status} (changed concurrently)")
    task.status, task.modified_at = new_status, now
    if new_status in (TaskStatus.CANCELLED.value, TaskStatus.DONE.value, TaskStatus.FAILED.value):
        from django_enrichment.services import task_progress_service

        task_progress_service.cancel_units(task)
    return task


def mark_in_progress(task: EnrichmentTask) -> EnrichmentTask:
    """Worker claims an open task (open -> in_progress) before resolving/proposing."""
    return transition_status(task, TaskStatus.IN_PROGRESS.value)


def mark_done(task: EnrichmentTask) -> EnrichmentTask:
    """Finish a claimed task (in_progress -> done). Call mark_in_progress first."""
    return transition_status(task, TaskStatus.DONE.value)


def increment_count(task: EnrichmentTask, key: str, by: int = 1) -> EnrichmentTask:
    """Atomically bump a progress counter in `task.counts` (e.g. 'proposed').

    Read-modify-write under a row lock: concurrent worker intake (n8n submits proposals in parallel,
    each calling this for the same task) would lose updates with a plain `counts[key] += 1` + `save()`
    (finding etap-03). `select_for_update` serialises the increment; the lock is held only for this row.
    """
    from django.db import transaction

    with transaction.atomic():
        locked = EnrichmentTask.objects.select_for_update().get(pk=task.pk)
        locked.counts[key] = locked.counts.get(key, 0) + by
        locked.save(update_fields=["counts", "modified_at"])
    task.counts = locked.counts
    return task


def record_error(
    task: EnrichmentTask, *, subject_ref: str, code: str, error_id: str, message: str = ""
) -> EnrichmentTask:
    """Remember why a subject failed (the first `ERRORS_CAP` failures; the counter keeps the total).
    A run-level stop carries a plain `message` too."""
    task.refresh_from_db(fields=["errors"])
    if len(task.errors) < ERRORS_CAP:
        entry = {"subject_ref": subject_ref, "code": code, "error_id": error_id}
        task.errors.append({**entry, "message": message} if message else entry)
        task.save(update_fields=["errors", "modified_at"])
    return task


def proposal_counts(task_ids: list[int]) -> dict[int, dict[str, int]]:
    """{task_id: {proposal status: n}} for the given tasks — one grouped query."""
    rows = (
        ContentProposal.objects.filter(task_id__in=task_ids)
        .values("task_id", "status")
        .annotate(n=Count("id"))
        .order_by()
    )
    counts: dict[int, dict[str, int]] = {task_id: {} for task_id in task_ids}
    for row in rows:
        counts[row["task_id"]][row["status"]] = row["n"]
    return counts
