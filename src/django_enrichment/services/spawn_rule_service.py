# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Spawn rules — operator config for gap-driven task spawning (etap-13, framework-agnostic).

One ``SpawnRule`` = one (module check → work-type) mapping with scope, limit and cooldown.
Two surfaces run a rule through ``run_rule``: the CMS "Run now" button (API) and the
``enrichment.gap_autospawn`` beat (rules with ``auto=True``). Dedup at this level: one
deterministic ``batch_key`` per rule plus an open-OR-in_progress lookup — the generic
``task_service.spawn`` coalesces only ``open`` tasks, which is not enough here (a gap task
can run for hours; the beat must not stack a second one behind a claimed task).
"""

from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Q, QuerySet

from django_enrichment.enums import TaskStatus
from django_enrichment.models import EnrichmentTask, SpawnRule
from django_enrichment.services import task_service

# Prefix of the deterministic per-rule batch_key. Owned here — the CMS never composes keys.
GAP_RULE_BATCH_PREFIX = "gaprule:"

_EDITABLE_FIELDS = frozenset(
    {"module", "check_key", "params", "scope", "task_type", "task_params", "limit", "cooldown_days", "auto", "active"}
)


def _spawn_default_limit() -> int:
    return getattr(settings, "ENRICHMENT_SPAWN_DEFAULT_LIMIT", 200)


def _spawn_cooldown_days() -> int:
    return getattr(settings, "ENRICHMENT_SPAWN_COOLDOWN_DAYS", 7)


def list_rules(*, active: bool | None = None, search: str | None = None) -> QuerySet[SpawnRule]:
    """Rules for the admin API, key order."""
    qs = SpawnRule.objects.all()
    if active is not None:
        qs = qs.filter(active=active)
    if search:
        qs = qs.filter(Q(key__icontains=search) | Q(check_key__icontains=search))
    return qs.order_by("key")


def get_rule(key: str) -> SpawnRule:
    """Fetch by key. Raises ``ValueError(... not found)`` so the API maps it to 404."""
    try:
        return SpawnRule.objects.get(key=key)
    except SpawnRule.DoesNotExist as exc:
        raise ValueError(f"Spawn rule {key!r} not found") from exc


def create_rule(
    *,
    key: str,
    module: str,
    check_key: str,
    task_type: str,
    params: dict | None = None,
    scope: dict | None = None,
    task_params: dict | None = None,
    limit: int | None = None,
    cooldown_days: int | None = None,
    auto: bool = False,
    active: bool = True,
) -> SpawnRule:
    """Create a rule. ``check_key`` is NOT validated against the module (decoupling) — an unknown
    key fails loud at resolve time, inside the adapter."""
    if SpawnRule.objects.filter(key=key).exists():
        raise ValueError(f"Spawn rule with key {key!r} already exists")
    return SpawnRule.objects.create(
        key=key,
        module=module,
        check_key=check_key,
        task_type=task_type,
        params=params or {},
        scope=scope or {},
        task_params=task_params or {},
        limit=limit,
        cooldown_days=cooldown_days,
        auto=auto,
        active=active,
    )


def update_rule(rule: SpawnRule, updates: dict[str, Any]) -> SpawnRule:
    """PATCH semantics with a mass-assignment whitelist. ``key`` is the immutable identifier."""
    invalid = set(updates) - _EDITABLE_FIELDS
    if invalid:
        raise ValueError(f"Fields not editable: {sorted(invalid)}")
    for field, value in updates.items():
        setattr(rule, field, value)
    rule.save()
    return rule


def delete_rule(rule: SpawnRule) -> None:
    rule.delete()


def batch_key_for(rule_key: str) -> str:
    return f"{GAP_RULE_BATCH_PREFIX}{rule_key}"


def find_running_task(rule_key: str) -> EnrichmentTask | None:
    """The rule's task that is still being worked (open OR claimed by a worker)."""
    return (
        EnrichmentTask.objects.filter(
            batch_key=batch_key_for(rule_key), status__in=(TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value)
        )
        .order_by("-created_at")
        .first()
    )


def run_rule(rule: SpawnRule, *, requested_by: AbstractBaseUser | None = None) -> EnrichmentTask | None:
    """Spawn the rule's gap task — or return the one already running, or ``None`` when there is
    nothing to do (probe of page 1, after the dedup/cooldown filter, came back empty)."""
    existing = find_running_task(rule.key)
    if existing is not None:
        return existing

    scope_spec = {
        "mode": "gap",
        "module": rule.module,
        "check": rule.check_key,
        "params": rule.params,
        "scope": rule.scope,
    }
    params = {
        **rule.task_params,
        "limit": rule.limit if rule.limit is not None else _spawn_default_limit(),
        "cooldown_days": rule.cooldown_days if rule.cooldown_days is not None else _spawn_cooldown_days(),
        "spawn_rule": rule.key,
    }
    if not task_service.resolve_gap_page(scope_spec, params, counts={}, page=1):
        return None
    return task_service.spawn(
        type=rule.task_type,
        scope_spec=scope_spec,
        params=params,
        requested_by=requested_by,
        batch_key=batch_key_for(rule.key),
    )
