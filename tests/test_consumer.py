# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Internal consumer: processes units and re-kicks itself while work remains; stops on a lost lease; budget stop."""

import pytest

from django_enrichment.models import ContentProposal, Worker, WorkUnit
from django_enrichment.services import (
    consumer_service,
    queue_dispatch,
    queue_reaper,
    unit_planner,
    work_queue_service,
)
from django_enrichment.tasks import worker_runner
from tests.test_runner import _spawn, echo_worker  # noqa: F401 — fixture

pytestmark = pytest.mark.django_db


@pytest.fixture
def kicks(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(queue_dispatch, "kick_consumer", lambda key, lane: sent.append((key, lane)))
    return sent


@pytest.fixture
def internal(echo_worker, settings):  # noqa: F811
    settings.ENRICHMENT_UNIT_SIZE = 1
    echo_worker()

    def _plan(refs, **params):
        task = _spawn(refs, **params)
        unit_planner.plan_task(task.pk)
        return task

    return _plan


def test_consume_task_processes_a_batch_and_kicks_itself_while_work_remains(internal, kicks, settings):
    settings.ENRICHMENT_CONSUME_MAX_UNITS = 2
    task = internal(["SKU-1", "SKU-2", "SKU-3"])
    kicks.clear()
    assert worker_runner.consume("pim.echo", task.lane) == 2
    assert kicks == [("pim.echo", task.lane)]
    assert ContentProposal.objects.count() == 2
    kicks.clear()
    assert worker_runner.consume("pim.echo", task.lane) == 1
    assert kicks == []


def test_a_failing_subject_does_not_stop_the_unit_or_the_consumer(internal, kicks):
    task = internal(["SKU-1", "SKU-2"], fail_on=["SKU-1"])
    assert consumer_service.consume("pim.echo", task.lane) == 2
    task.refresh_from_db()
    assert (task.status, task.counts["failed"], task.counts["proposed"]) == ("done", 1, 1)


def test_a_lost_lease_stops_the_consumer(internal, kicks, monkeypatch):
    task = internal(["SKU-1", "SKU-2"])

    def lose(token):
        raise work_queue_service.LeaseLost("gone")

    monkeypatch.setattr(work_queue_service, "heartbeat", lose)
    assert consumer_service.consume("pim.echo", task.lane) == 1  # first unit handled, then it stopped
    assert ContentProposal.objects.count() == 0
    assert WorkUnit.objects.filter(status="leased").count() == 1


def test_a_budget_error_requeues_the_unit_and_pauses_the_worker(internal, kicks):
    task = internal(["SKU-1"])
    from django_utils.toolbox import ToolboxError

    from tests import test_runner

    def boom(self, worker, task, subject_ref):
        raise ToolboxError(402, "limit", code="BUDGET_EXCEEDED")

    test_runner.EchoKind.run, original = boom, test_runner.EchoKind.run
    try:
        consumer_service.consume("pim.echo", task.lane)
    finally:
        test_runner.EchoKind.run = original
    unit = WorkUnit.objects.get()
    assert unit.status == "queued" and unit.last_error["code"] == "BUDGET_EXCEEDED"
    assert Worker.objects.get(key="pim.echo").paused_reason


def test_reaper_restarts_consumers_where_work_waits_and_nothing_is_leased(internal, kicks):
    internal(["SKU-1"])
    kicks.clear()
    assert queue_reaper.reap()["kicked"] == 1
    assert len(kicks) == 1
    work_queue_service.claim("pim.echo", "x")
    kicks.clear()
    assert queue_reaper.reap()["kicked"] == 0


def test_the_old_run_task_message_plans_the_task(internal, kicks, echo_worker):  # noqa: F811
    task = _spawn(["SKU-1"])
    assert worker_runner.run_task(task.pk) == 1
    assert task.work_units.count() == 1
