# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Operator runs (`POST tasks/`) — skip subjects that already have the same run in flight, then spawn.

The same run = same task type, template, channel, fields and languages (the fingerprint). In flight = an
`open` or `in_progress` task. Only list-mode scopes carry subjects; other scopes and analyzer calls with a
`batch_key` (which coalesce on their own) spawn unchanged. Pending proposals keep their supersede behaviour.
"""

from dataclasses import dataclass, field

from django.contrib.auth.models import AbstractBaseUser

from django_enrichment.enums import TaskStatus
from django_enrichment.models import EnrichmentTask
from django_enrichment.schemas.requests import TaskSpawnRequest
from django_enrichment.services import task_service

_IN_FLIGHT = (TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value)


class AlreadyRunningError(Exception):
    """Every subject of the run already has the same run in flight — nothing was spawned."""


@dataclass(frozen=True)
class SpawnResult:
    task: EnrichmentTask
    skipped_in_flight: list[str] = field(default_factory=list)


def spawn(order: TaskSpawnRequest, *, requested_by: AbstractBaseUser | None = None) -> SpawnResult:
    """Spawn `order` without the subjects already in flight. Raises `AlreadyRunningError` (all skipped), `ValueError`."""
    scope_spec, skipped = _drop_in_flight(order)
    task = task_service.spawn(
        type=order.type,
        scope_spec=scope_spec,
        params=order.params,
        requested_by=requested_by,
        batch_key=order.batch_key,
        template=order.template,
        lane=order.lane or "",
    )
    return SpawnResult(task=task, skipped_in_flight=skipped)


def _drop_in_flight(order: TaskSpawnRequest) -> tuple[dict, list[str]]:
    scope_spec = order.scope_spec
    refs = scope_spec.get("refs")
    if order.batch_key or scope_spec.get("mode") != "list" or not refs:
        return scope_spec, []
    task_type, params = task_service.resolve_type_and_params(order.type, order.params, order.template)
    busy = _busy_refs(task_type, _fingerprint(params, scope_spec), scope_spec.get("module"))
    skipped = [ref for ref in refs if ref in busy]
    if len(skipped) == len(refs):
        raise AlreadyRunningError(_already_running_message(len(refs)))
    return {**scope_spec, "refs": [ref for ref in refs if ref not in busy]}, skipped


def _busy_refs(task_type: str, fingerprint: tuple, module: str | None) -> set[str]:
    """Refs of the in-flight list-mode tasks of `task_type` whose fingerprint matches."""
    rows = EnrichmentTask.objects.filter(
        type=task_type, status__in=_IN_FLIGHT, scope_spec__mode="list", scope_spec__module=module
    ).values_list("params", "scope_spec")
    busy: set[str] = set()
    for params, scope_spec in rows:
        if _fingerprint(params, scope_spec) == fingerprint:
            busy.update(scope_spec.get("refs", []))
    return busy


def _fingerprint(params: dict, scope_spec: dict) -> tuple:
    """What makes two runs "the same": template, channel, fields and languages (the type is matched by the query)."""
    return (
        params.get("template", ""),
        params.get("channel") or scope_spec.get("channel", ""),
        _values(params, "fields", "field"),
        _values(params, "languages", "target_languages"),
    )


def _values(params: dict, *keys: str) -> tuple[str, ...]:
    """The first present key as a sorted tuple (a list value, or a single string)."""
    for key in keys:
        if (value := params.get(key)) is not None:
            return tuple(sorted(value)) if isinstance(value, list) else (str(value),)
    return ()


def _already_running_message(count: int) -> str:
    if count == 1:
        return "This run is already in progress for this item. Wait for it to finish."
    return f"This run is already in progress for all {count} items. Wait for it to finish."
