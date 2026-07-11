# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Spawn rules (etap-13) — CRUD whitelist, the run_rule dedup (open AND in_progress), and the
probe-before-spawn behaviour. Uses the in-memory fake adapter; candidates are seeded through
`rule.scope["candidates"]` (the fake's `find_gaps` echoes them)."""

import pytest
from django.test import override_settings

from django_enrichment.enums import TaskStatus
from django_enrichment.models import EnrichmentTask, SpawnRule
from django_enrichment.services import spawn_rule_service, task_service


def _candidates(*refs):
    return [{"subject_ref": r, "target_kind": "attribute_value", "target_module": "pim"} for r in refs]


def _rule(key="r1", *, scope=None, **kwargs):
    defaults = {"module": "pim", "check_key": "pl-description", "task_type": "fix-attribute"}
    defaults.update(kwargs)
    return spawn_rule_service.create_rule(
        key=key, scope=scope if scope is not None else {"candidates": _candidates("S1")}, **defaults
    )


# ── CRUD ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_create_rule_defaults():
    rule = _rule()
    assert rule.auto is False
    assert rule.active is True
    assert rule.limit is None
    assert rule.cooldown_days is None


@pytest.mark.django_db
def test_create_rule_duplicate_key_raises():
    _rule()
    with pytest.raises(ValueError, match="already exists"):
        _rule()


@pytest.mark.django_db
def test_get_rule_not_found_raises_value_error():
    with pytest.raises(ValueError, match="not found"):
        spawn_rule_service.get_rule("nope")


@pytest.mark.django_db
def test_update_rule_whitelist_rejects_key():
    rule = _rule()
    with pytest.raises(ValueError, match="not editable"):
        spawn_rule_service.update_rule(rule, {"key": "new-key"})


@pytest.mark.django_db
def test_update_rule_whitelist_rejects_unknown_field():
    rule = _rule()
    with pytest.raises(ValueError, match="not editable"):
        spawn_rule_service.update_rule(rule, {"status": "x"})


@pytest.mark.django_db
def test_update_rule_applies_editable_fields():
    rule = _rule()
    updated = spawn_rule_service.update_rule(rule, {"auto": True, "limit": 50})
    assert updated.auto is True
    assert updated.limit == 50


@pytest.mark.django_db
def test_list_rules_filters_active_and_search():
    _rule(key="alpha")
    inactive = _rule(key="beta", active=False)

    assert [r.key for r in spawn_rule_service.list_rules(active=True)] == ["alpha"]
    assert [r.key for r in spawn_rule_service.list_rules(search="bet")] == [inactive.key]


# ── run_rule ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_run_rule_spawns_gap_task_with_rule_shape(fake_adapter):
    rule = _rule(limit=10, cooldown_days=3, task_params={"feature": "description"})

    task = spawn_rule_service.run_rule(rule)

    assert task is not None
    assert task.type == "fix-attribute"
    assert task.batch_key == "gaprule:r1"
    assert task.scope_spec == {
        "mode": "gap",
        "module": "pim",
        "check": "pl-description",
        "params": {},
        "scope": {"candidates": _candidates("S1")},
    }
    assert task.params == {"feature": "description", "limit": 10, "cooldown_days": 3, "spawn_rule": "r1"}


@pytest.mark.django_db
@override_settings(ENRICHMENT_SPAWN_DEFAULT_LIMIT=77, ENRICHMENT_SPAWN_COOLDOWN_DAYS=9)
def test_run_rule_null_limit_cooldown_resolve_from_settings(fake_adapter):
    rule = _rule()

    task = spawn_rule_service.run_rule(rule)

    assert task.params["limit"] == 77
    assert task.params["cooldown_days"] == 9


@pytest.mark.django_db
def test_run_rule_returns_existing_open_task(fake_adapter):
    rule = _rule()
    first = spawn_rule_service.run_rule(rule)

    second = spawn_rule_service.run_rule(rule)

    assert second.pk == first.pk
    assert EnrichmentTask.objects.count() == 1


@pytest.mark.django_db
def test_run_rule_coalesces_with_in_progress_task(fake_adapter):
    # The generic spawn() only coalesces OPEN tasks — a claimed (in_progress) gap task can run
    # for hours, and the beat must not stack a second one behind it.
    rule = _rule()
    first = spawn_rule_service.run_rule(rule)
    task_service.mark_in_progress(first)

    second = spawn_rule_service.run_rule(rule)

    assert second.pk == first.pk
    assert EnrichmentTask.objects.count() == 1


@pytest.mark.django_db
def test_run_rule_respawns_after_done(fake_adapter):
    rule = _rule()
    first = spawn_rule_service.run_rule(rule)
    task_service.mark_in_progress(first)
    task_service.mark_done(first)

    second = spawn_rule_service.run_rule(rule)

    assert second.pk != first.pk


@pytest.mark.django_db
def test_run_rule_probe_empty_returns_none(fake_adapter):
    rule = _rule(scope={"candidates": []})

    assert spawn_rule_service.run_rule(rule) is None
    assert EnrichmentTask.objects.count() == 0


@pytest.mark.django_db
def test_find_running_task_ignores_terminal_states(fake_adapter):
    rule = _rule()
    task = spawn_rule_service.run_rule(rule)
    task_service.transition_status(task, TaskStatus.CANCELLED.value)

    assert spawn_rule_service.find_running_task(rule.key) is None


@pytest.mark.django_db
def test_delete_rule_removes_row():
    rule = _rule()
    spawn_rule_service.delete_rule(rule)
    assert SpawnRule.objects.count() == 0
