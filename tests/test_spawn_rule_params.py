# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""A spawn rule's `limit`, `cooldown_days` and key reach the task of a worker that owns its type.

Kind `Params` drop the keys they do not declare; the gap budget and `spawn_rule` are bus-level and must survive
that validation, or "Run now" takes every gap on the rule and the task forgets which rule spawned it. The `echo`
kind of `test_runner` declares none of them, like the real kinds.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal
from django_enrichment.services import spawn_rule_service, task_service, unit_planner
from tests.queue_helpers import run_queue
from tests.test_runner import echo_worker  # noqa: F401 — fixture

pytestmark = pytest.mark.django_db

RULES = "/api/enrichment/v2/admin/spawn-rules/"
TASKS = "/api/enrichment/v2/admin/tasks/"


def _candidates(count: int) -> list[dict]:
    return [
        {"subject_ref": f"S{n}", "target_kind": "attribute_value", "target_module": "pim"} for n in range(1, count + 1)
    ]


def _rule(candidates: int = 18, **over):
    return spawn_rule_service.create_rule(
        key="r1",
        module="pim",
        check_key="c",
        task_type="pim.echo",
        scope={"candidates": _candidates(candidates)},
        **over,
    )


def _planned_refs(task) -> list[str]:
    unit_planner.plan_task(task.pk)
    units = task.work_units.order_by("pk").values_list("subject_refs", flat=True)
    return [ref for refs in units for ref in refs]


def _bus_params(params: dict) -> dict:
    """The gap budget and the rule key out of a task's params (the kind adds its own defaults around them)."""
    return {key: params[key] for key in ("limit", "cooldown_days", "spawn_rule") if key in params}


def _rejected(ref: str, *, days_ago: int) -> None:
    ContentProposal.objects.create(
        target_module="pim",
        subject_ref=ref,
        target_kind="attribute_value",
        target_locator={},
        proposed_value={"text": "x"},
        status=ProposalStatus.REJECTED.value,
        reviewed_at=timezone.now() - timedelta(days=days_ago),
    )


def test_a_run_plans_no_more_subjects_than_the_rules_limit(echo_worker):  # noqa: F811
    echo_worker()
    task = spawn_rule_service.run_rule(_rule(limit=5))

    assert _planned_refs(task) == ["S1", "S2", "S3", "S4", "S5"]


def test_a_run_proposes_for_the_limit_and_closes(echo_worker):  # noqa: F811
    echo_worker()
    task = run_queue(spawn_rule_service.run_rule(_rule(limit=7)))

    assert task.status == "done"
    assert task.counts == {"proposed": 7}
    assert ContentProposal.objects.count() == 7


def test_a_run_without_a_rule_limit_uses_the_module_default(echo_worker, settings):  # noqa: F811
    settings.ENRICHMENT_SPAWN_DEFAULT_LIMIT = 3
    echo_worker()
    task = spawn_rule_service.run_rule(_rule())

    assert len(_planned_refs(task)) == 3


def test_a_run_keeps_a_target_inside_the_rules_cooldown_out(echo_worker):  # noqa: F811
    _rejected("S1", days_ago=8)  # the module default (7 days) would let S1 back in
    echo_worker()
    task = spawn_rule_service.run_rule(_rule(candidates=3, cooldown_days=10))

    assert _planned_refs(task) == ["S2", "S3"]


def test_a_run_lets_a_target_back_in_after_the_rules_cooldown(echo_worker):  # noqa: F811
    _rejected("S1", days_ago=2)  # the module default (7 days) would keep S1 out
    echo_worker()
    task = spawn_rule_service.run_rule(_rule(candidates=3, cooldown_days=1))

    assert _planned_refs(task) == ["S1", "S2", "S3"]


def test_a_run_records_its_rule_and_budget_on_the_task(echo_worker):  # noqa: F811
    echo_worker()
    task = spawn_rule_service.run_rule(_rule(limit=5, cooldown_days=3))
    task.refresh_from_db()

    assert _bus_params(task.params) == {"limit": 5, "cooldown_days": 3, "spawn_rule": "r1"}


def test_the_rules_task_params_still_go_through_the_kinds_validation(echo_worker):  # noqa: F811
    echo_worker()
    task = spawn_rule_service.run_rule(_rule(limit=5, task_params={"channel": "shop", "unknown": 1}))

    assert task.params["channel"] == "shop"
    assert "unknown" not in task.params
    assert _bus_params(task.params) == {"limit": 5, "cooldown_days": 7, "spawn_rule": "r1"}


def test_a_gap_task_from_a_client_may_set_its_own_budget(echo_worker):  # noqa: F811
    echo_worker()
    scope_spec = {"mode": "gap", "module": "pim", "check": "c", "scope": {"candidates": _candidates(18)}}
    task = task_service.spawn(type="pim.echo", scope_spec=scope_spec, params={"limit": 2, "cooldown_days": 1})

    assert task.params["limit"] == 2
    assert len(_planned_refs(task)) == 2


@pytest.mark.parametrize(
    ("params", "field"),
    [({"limit": "many"}, "limit"), ({"limit": -1}, "limit"), ({"cooldown_days": None}, "cooldown_days")],
)
def test_a_malformed_budget_is_refused_at_spawn(echo_worker, params, field):  # noqa: F811
    echo_worker()

    with pytest.raises(ValueError, match=f"params invalid: {field}"):
        task_service.spawn(type="pim.echo", scope_spec={"mode": "list", "module": "pim", "refs": ["S1"]}, params=params)


def test_the_run_endpoint_plans_the_rules_limit(admin_client, echo_worker, django_capture_on_commit_callbacks):  # noqa: F811
    echo_worker()
    _rule(limit=5)

    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(f"{RULES}r1/run/")

    assert response.status_code == 201
    task = response.json()["task"]
    assert _bus_params(task["params"]) == {"limit": 5, "cooldown_days": 7, "spawn_rule": "r1"}
    planned = task_service.get_task(task["id"]).work_units.values_list("subject_refs", flat=True)
    assert sum(len(refs) for refs in planned) == 5
    assert ContentProposal.objects.count() == 5


def test_a_malformed_budget_in_a_task_create_is_a_400(admin_client, echo_worker):  # noqa: F811
    echo_worker()
    body = {
        "type": "pim.echo",
        "scope_spec": {"mode": "list", "module": "pim", "refs": ["S1"]},
        "params": {"limit": "x"},
    }

    response = admin_client.post(TASKS, body, format="json")

    assert response.status_code == 400
