# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The gap_autospawn beat (etap-13) — eager Celery via the autouse conftest fixture.

One dumb loop over auto+active rules: spawned / already_running / empty / failed buckets,
and a broken rule (adapter raising) must not kill the rest of the pass."""

import pytest
from django.test import override_settings

from django_enrichment.models import EnrichmentTask
from django_enrichment.services import spawn_rule_service
from django_enrichment.tasks import gap_autospawn_task


def _candidates(*refs):
    return [{"subject_ref": r, "target_kind": "attribute_value", "target_module": "pim"} for r in refs]


def _rule(key, *, auto=True, active=True, scope=None, module="pim"):
    return spawn_rule_service.create_rule(
        key=key,
        module=module,
        check_key="pl-description",
        task_type="fix-attribute",
        scope=scope if scope is not None else {"candidates": _candidates("S1")},
        auto=auto,
        active=active,
    )


@pytest.mark.django_db
def test_autospawn_spawns_for_auto_active_rules(fake_adapter):
    _rule("a1")

    summary = gap_autospawn_task.delay().get()

    assert summary["rules"] == 1
    assert summary["spawned"] == ["a1"]
    task = EnrichmentTask.objects.get()
    assert task.batch_key == "gaprule:a1"


@pytest.mark.django_db
def test_autospawn_skips_manual_and_inactive_rules(fake_adapter):
    _rule("manual", auto=False)
    _rule("inactive", active=False)

    summary = gap_autospawn_task.delay().get()

    assert summary["rules"] == 0
    assert EnrichmentTask.objects.count() == 0


@pytest.mark.django_db
def test_autospawn_already_running_bucket(fake_adapter):
    rule = _rule("a1")
    spawn_rule_service.run_rule(rule)

    summary = gap_autospawn_task.delay().get()

    assert summary["already_running"] == ["a1"]
    assert EnrichmentTask.objects.count() == 1


@pytest.mark.django_db
def test_autospawn_empty_probe_bucket(fake_adapter):
    _rule("a1", scope={"candidates": []})

    summary = gap_autospawn_task.delay().get()

    assert summary["empty"] == ["a1"]
    assert EnrichmentTask.objects.count() == 0


@pytest.mark.django_db
def test_autospawn_failed_rule_does_not_kill_the_pass(fake_adapter):
    # An unregistered module makes get_adapter raise — the loop must log, bucket it, and go on.
    with override_settings(ENRICHMENT_ADAPTERS={"pim": "tests.fake_adapter"}):
        _rule("broken", module="no-such-module")
        _rule("works")

        summary = gap_autospawn_task.delay().get()

    assert summary["failed"] == ["broken"]
    assert summary["spawned"] == ["works"]
    assert EnrichmentTask.objects.count() == 1
