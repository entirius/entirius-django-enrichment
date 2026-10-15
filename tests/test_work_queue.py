# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Work queue services: planning, claim / lease / heartbeat / complete / fail, reaper, pause, task status, guards."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from django_enrichment.enums import RevisionAuthor, TaskStatus
from django_enrichment.kinds import ProposalDraft
from django_enrichment.models import ContentProposal, EnrichmentTask, Worker, WorkerInstance, WorkUnit
from django_enrichment.services import (
    cost_guard_service,
    proposal_service,
    queue_escalation,
    queue_reaper,
    queue_status,
    revision_service,
    task_service,
    unit_planner,
    unit_result_service,
    work_queue_service,
)
from django_enrichment.services.queue_types import LeaseLost, SubjectResult
from tests.test_runner import _spawn, echo_worker  # noqa: F401 — fixture

pytestmark = pytest.mark.django_db
KEY = "pim.echo"


@pytest.fixture
def notes(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(queue_escalation, "notify", lambda **kw: sent.append(kw) or True)
    return sent


@pytest.fixture
def planned(echo_worker):  # noqa: F811
    """A planned task of 5 subjects in units of 2 (3 units), external-style: nothing consumes it."""

    def _make(refs=("SKU-1", "SKU-2", "SKU-3", "SKU-4", "SKU-5"), **params):
        if not Worker.objects.filter(key=KEY).exists():
            echo_worker(mode="external")
        task = _spawn(list(refs), **params)
        unit_planner.plan_task(task.pk)
        return task

    return _make


@pytest.fixture(autouse=True)
def small_units(settings):
    settings.ENRICHMENT_UNIT_SIZE = 2


def _ok(*refs: str) -> list[SubjectResult]:
    return [
        SubjectResult(r, [ProposalDraft("attribute_value", {"language": "pl"}, {"text": r}, subject_label=r)])
        for r in refs
    ]


class TestPlanning:
    def test_targets_are_split_into_small_units(self, planned):
        task = planned()
        assert [u.subject_refs for u in task.units.order_by("pk")] == [
            ["SKU-1", "SKU-2"],
            ["SKU-3", "SKU-4"],
            ["SKU-5"],
        ]
        assert {u.lane for u in task.units.all()} == {task.lane}

    def test_planning_twice_adds_nothing(self, planned):
        task = planned()
        assert unit_planner.plan_task(task.pk) == 0
        assert task.units.count() == 3

    def test_planner_stops_at_the_subject_cap(self, echo_worker, settings):  # noqa: F811
        echo_worker(mode="external")
        task = _spawn(["SKU-1", "SKU-2", "SKU-3", "SKU-4", "SKU-5"])
        settings.ENRICHMENT_QUEUE_MAX_SUBJECTS = 3
        unit_planner.plan_task(task.pk)
        assert task.units.count() == 2

    def test_empty_task_is_done(self, planned):
        assert planned(refs=()).units.count() == 0
        assert EnrichmentTask.objects.get().status == TaskStatus.DONE.value

    def test_spawn_over_the_queue_cap_is_refused(self, echo_worker, settings):  # noqa: F811
        echo_worker()
        settings.ENRICHMENT_QUEUE_MAX_SUBJECTS = 2
        with pytest.raises(ValueError, match="at most 2 items"):
            task_service.spawn(
                type=KEY, scope_spec={"mode": "list", "module": "pim", "refs": ["a", "b", "c"]}, lane="queue"
            )


class TestClaim:
    def test_claim_leases_one_unit_and_starts_the_task(self, planned):
        task = planned()
        unit = work_queue_service.claim(KEY, "n8n-1", "1.0")
        assert (unit.status, unit.attempts, unit.leased_by) == ("leased", 1, "n8n-1")
        assert unit.lease_token and unit.lease_until > timezone.now()
        task.refresh_from_db()
        assert task.status == TaskStatus.IN_PROGRESS.value
        assert WorkerInstance.objects.get(instance="n8n-1").version == "1.0"

    def test_in_flight_cap(self, planned, settings):
        planned()
        settings.ENRICHMENT_MAX_INFLIGHT_PER_WORKER = 2
        assert work_queue_service.claim(KEY, "a") and work_queue_service.claim(KEY, "b")
        assert work_queue_service.claim(KEY, "c") is None

    def test_paused_worker_hands_out_nothing(self, planned):
        planned()
        Worker.objects.filter(key=KEY).update(paused_reason="paused for test")
        assert work_queue_service.claim(KEY, "a") is None
        assert WorkerInstance.objects.filter(instance="a").exists()  # still seen = online

    def test_unknown_or_inactive_worker(self, planned):
        planned()
        with pytest.raises(ValueError, match="not found"):
            work_queue_service.claim("nope", "a")
        Worker.objects.filter(key=KEY).update(active=False)
        with pytest.raises(ValueError, match="not found"):
            work_queue_service.claim(KEY, "a")

    def test_interactive_lane_first(self, planned):
        slow = planned()
        fast = _spawn(["SKU-9"], lane="now")
        unit_planner.plan_task(fast.pk)
        assert work_queue_service.claim(KEY, "a").task_id == fast.pk
        assert slow.units.filter(status="queued").count() == 3

    def test_lane_filter(self, planned):
        task = planned()
        assert work_queue_service.claim(KEY, "a", lane="now" if task.lane == "queue" else "queue") is None

    def test_backoff_hides_a_unit_until_due(self, planned):
        planned(refs=("SKU-1",))
        unit = work_queue_service.claim(KEY, "a")
        work_queue_service.fail(unit.lease_token, "X", "no", retryable=True)
        assert work_queue_service.claim(KEY, "a") is None
        WorkUnit.objects.update(not_before=timezone.now() - timedelta(seconds=1))
        assert work_queue_service.claim(KEY, "a").attempts == 2

    def test_cancelled_task_units_are_not_handed_out(self, planned):
        task = planned()
        task_service.transition_status(task, TaskStatus.CANCELLED.value)
        assert work_queue_service.claim(KEY, "a") is None
        assert set(task.units.values_list("status", flat=True)) == {"cancelled"}


class TestLease:
    def test_heartbeat_extends_and_expired_lease_is_lost(self, planned):
        planned()
        unit = work_queue_service.claim(KEY, "a")
        WorkUnit.objects.filter(pk=unit.pk).update(lease_until=timezone.now() + timedelta(seconds=5))
        assert work_queue_service.heartbeat(unit.lease_token).lease_until > timezone.now() + timedelta(seconds=100)
        WorkUnit.objects.filter(pk=unit.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        with pytest.raises(LeaseLost):
            work_queue_service.heartbeat(unit.lease_token)

    @pytest.mark.parametrize("token", [uuid.uuid4(), "not-a-uuid"])
    def test_unknown_token_is_lost(self, planned, token):
        planned()
        for call in (work_queue_service.heartbeat, lambda t: unit_result_service.complete(t, [])):
            with pytest.raises(LeaseLost):
                call(token)

    def test_reaper_requeues_an_expired_lease_and_the_old_token_dies(self, planned):
        planned()
        unit = work_queue_service.claim(KEY, "a")
        old = unit.lease_token
        WorkUnit.objects.filter(pk=unit.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        assert queue_reaper.reap()["expired"] == 1
        unit.refresh_from_db()
        assert (unit.status, unit.attempts, unit.lease_token) == ("queued", 1, None)
        with pytest.raises(LeaseLost):
            unit_result_service.complete(old, _ok("SKU-1", "SKU-2"))

    def test_reaper_fails_a_unit_out_of_attempts_and_notifies(self, planned, notes, settings):
        task = planned(refs=("SKU-1",))
        settings.ENRICHMENT_MAX_ATTEMPTS = 1
        unit = work_queue_service.claim(KEY, "a")
        WorkUnit.objects.filter(pk=unit.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        queue_reaper.reap()
        unit.refresh_from_db()
        assert unit.status == "failed" and unit.last_error["code"] == "LEASE_EXPIRED"
        assert [n["subject_ref"] for n in notes] == [f"enrichment:task:{task.pk}"]
        task.refresh_from_db()
        assert (task.status, task.counts["failed"]) == (TaskStatus.FAILED.value, 1)

    def test_events_are_capped(self, planned, settings):
        settings.ENRICHMENT_EVENTS_CAP = 3
        planned(refs=("SKU-1",))
        for _ in range(3):
            unit = work_queue_service.claim(KEY, "a")
            work_queue_service.fail(unit.lease_token, "X", "no")
            WorkUnit.objects.update(not_before=None)
        assert len(WorkUnit.objects.get().events) == 3


class TestComplete:
    def test_results_become_proposals_and_the_task_finishes(self, planned):
        task = planned(refs=("SKU-1", "SKU-2"))
        unit = work_queue_service.claim(KEY, "a")
        outcome = unit_result_service.complete(unit.lease_token, _ok("SKU-1", "SKU-2"))
        assert outcome == {"succeeded": 2, "failed": 0, "kept_edited": 0}
        assert ContentProposal.objects.filter(task=task).count() == 2
        task.refresh_from_db()
        assert (task.status, task.counts["proposed"]) == (TaskStatus.DONE.value, 2)

    def test_complete_is_idempotent_per_token(self, planned):
        planned(refs=("SKU-1", "SKU-2"))
        unit = work_queue_service.claim(KEY, "a")
        first = unit_result_service.complete(unit.lease_token, _ok("SKU-1", "SKU-2"))
        assert unit_result_service.complete(unit.lease_token, []) == first
        assert ContentProposal.objects.count() == 2

    def test_missing_and_failed_subjects_are_counted_and_recorded(self, planned):
        task = planned(refs=("SKU-1", "SKU-2", "SKU-3"))
        unit = work_queue_service.claim(KEY, "a")  # SKU-1, SKU-2
        results = [SubjectResult("SKU-1", error_code="TIMEOUT", error_id="abc")]
        assert unit_result_service.complete(unit.lease_token, results)["failed"] == 2
        task.refresh_from_db()
        assert [(e["subject_ref"], e["code"]) for e in task.errors] == [("SKU-1", "TIMEOUT"), ("SKU-2", "NO_RESULT")]

    def test_result_for_a_foreign_subject_is_refused(self, planned):
        planned()
        unit = work_queue_service.claim(KEY, "a")
        with pytest.raises(ValueError, match="outside the unit"):
            unit_result_service.complete(unit.lease_token, _ok("SKU-9"))

    def test_task_status_is_derived_from_all_units(self, planned):
        task = planned()
        for _ in range(3):
            unit = work_queue_service.claim(KEY, "a")
            task.refresh_from_db()
            assert task.status == TaskStatus.IN_PROGRESS.value
            unit_result_service.complete(unit.lease_token, _ok(*unit.subject_refs))
        task.refresh_from_db()
        assert task.status == TaskStatus.DONE.value

    def test_results_after_a_cancel_are_discarded(self, planned):
        task = planned(refs=("SKU-1",))
        unit = work_queue_service.claim(KEY, "a")
        task_service.transition_status(task_service.get_task(task.pk), TaskStatus.CANCELLED.value)
        assert unit_result_service.complete(unit.lease_token, _ok("SKU-1"))["discarded"] is True
        assert not ContentProposal.objects.exists()
        task.refresh_from_db()
        assert task.status == TaskStatus.CANCELLED.value


class TestFailAndPause:
    def test_retryable_fail_goes_back_with_a_backoff_then_fails_for_good(self, planned, notes, settings):
        settings.ENRICHMENT_MAX_ATTEMPTS = 2
        task = planned(refs=("SKU-1",))
        unit = work_queue_service.claim(KEY, "a")
        unit = work_queue_service.fail(unit.lease_token, "TIMEOUT", "Too slow.")
        assert unit.status == "queued" and unit.not_before > timezone.now()
        WorkUnit.objects.update(not_before=None)
        unit = work_queue_service.fail(work_queue_service.claim(KEY, "a").lease_token, "TIMEOUT", "Too slow.")
        assert unit.status == "failed" and len(notes) == 1
        task.refresh_from_db()
        assert task.status == TaskStatus.FAILED.value

    def test_non_retryable_fails_at_once_and_repeat_is_idempotent(self, planned, notes):
        planned(refs=("SKU-1",))
        unit = work_queue_service.claim(KEY, "a")
        work_queue_service.fail(unit.lease_token, "BAD", "Bad input.", retryable=False)
        assert work_queue_service.fail(unit.lease_token, "BAD", "Bad input.", retryable=False).status == "failed"
        assert len(notes) == 1

    def test_auto_pause_after_n_failures_in_a_row(self, planned, notes, settings):
        settings.ENRICHMENT_PAUSE_AFTER_FAILURES = 2
        planned()
        for _ in range(2):
            work_queue_service.fail(work_queue_service.claim(KEY, "a").lease_token, "X", "no", retryable=False)
        assert Worker.objects.get(key=KEY).paused_reason
        assert any(n["title"].endswith("work paused") for n in notes)
        assert work_queue_service.claim(KEY, "a") is None

    def test_auto_pause_off_only_notifies(self, planned, notes, settings):
        settings.ENRICHMENT_PAUSE_AFTER_FAILURES = 1
        settings.ENRICHMENT_AUTO_PAUSE = False
        planned()
        work_queue_service.fail(work_queue_service.claim(KEY, "a").lease_token, "X", "no", retryable=False)
        assert Worker.objects.get(key=KEY).paused_reason == ""
        assert any(n["title"].endswith("work paused") for n in notes)

    def test_a_completed_unit_resets_the_streak(self, planned, settings):
        settings.ENRICHMENT_PAUSE_AFTER_FAILURES = 2
        planned()
        work_queue_service.fail(work_queue_service.claim(KEY, "a").lease_token, "X", "no", retryable=False)
        unit = work_queue_service.claim(KEY, "a")
        unit_result_service.complete(unit.lease_token, _ok(*unit.subject_refs))
        work_queue_service.fail(work_queue_service.claim(KEY, "a").lease_token, "X", "no", retryable=False)
        assert Worker.objects.get(key=KEY).paused_reason == ""

    def test_budget_error_pauses_at_once_and_resume_clears_it(self, planned, notes):
        planned()
        work_queue_service.fail(work_queue_service.claim(KEY, "a").lease_token, "BUDGET_EXCEEDED", "Limit.")
        assert "budget" in Worker.objects.get(key=KEY).paused_reason
        row = next(r for r in queue_status.workers_status() if r["key"] == KEY)
        assert (row["state"], row["queued_units"]) == ("paused", 3)
        queue_status.resume(KEY)
        assert Worker.objects.get(key=KEY).paused_reason == ""


class TestEscalation:
    def test_missing_notifications_channel_is_only_logged(self, caplog):
        caplog.set_level("INFO", logger="enrichment.queue")
        assert queue_escalation.notify(subject_ref="x", title="t", body="b") is False
        assert "notify_skipped" in caplog.text

    def test_stale_queue_without_an_online_instance_notifies(self, planned, notes, settings):
        planned()
        settings.ENRICHMENT_STALE_QUEUE_S = 10
        WorkUnit.objects.update(created_at=timezone.now() - timedelta(seconds=60))
        assert queue_escalation.check_stale_queue() == 1
        assert notes[0]["title"].startswith("Nobody is taking")
        work_queue_service.claim(KEY, "a")  # an instance shows up
        assert queue_escalation.check_stale_queue() == 0

    def test_status_row(self, planned):
        planned()
        work_queue_service.claim(KEY, "a")
        row = next(r for r in queue_status.workers_status() if r["key"] == KEY)
        assert (row["queued_units"], row["leased_units"], row["online_instances"]) == (2, 1, 1)
        assert row["oldest_queued_age_s"] >= 0


class TestGuards:
    def test_cost_limit_stops_a_task_and_notifies_once(self, planned, notes, settings):
        settings.ENRICHMENT_TASK_MAX_COST_USD = "1.00"
        task = planned()
        EnrichmentTask.objects.filter(pk=task.pk).update(cost_usd=Decimal("1.50"))
        assert work_queue_service.claim(KEY, "a") is None
        task.refresh_from_db()
        assert set(task.units.values_list("status", flat=True)) == {"cancelled"}
        assert task.errors[0]["message"] == cost_guard_service.MESSAGE
        assert len(notes) == 1
        assert cost_guard_service.stop_over_cost() == 0

    def test_cost_below_the_limit_is_untouched(self, planned):
        task = planned()
        EnrichmentTask.objects.filter(pk=task.pk).update(cost_usd=Decimal("0.50"))
        assert work_queue_service.claim(KEY, "a")

    def test_operator_edited_subject_is_skipped_before_the_paid_call(self, planned, monkeypatch):
        from django_enrichment.services import consumer_service

        task = planned(refs=("SKU-1",))
        Worker.objects.filter(key=KEY).update(mode="internal")
        proposal = proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator={"language": "pl"},
            proposed_value={"text": "ai"},
            source="worker:pim.echo@v1",
        )
        ran = []
        monkeypatch.setattr("tests.test_runner.EchoKind.run", lambda *a: ran.append(a) or [])
        _edit(proposal)
        consumer_service.consume(KEY, task.lane)
        assert ran == []
        task.refresh_from_db()
        assert task.counts["kept_edited"] == 1

    def test_an_edit_landing_inside_intake_is_never_superseded(self, planned):
        proposal = proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator={"language": "pl"},
            proposed_value={"text": "ai"},
        )
        _edit(proposal)
        with pytest.raises(proposal_service.KeptEdited):
            proposal_service.intake(
                target_module="pim",
                subject_ref="SKU-1",
                target_kind="attribute_value",
                target_locator={"language": "pl"},
                proposed_value={"text": "new"},
                keep_edited=True,
            )
        proposal.refresh_from_db()
        assert proposal.status == "pending"
        assert ContentProposal.objects.count() == 1


def _edit(proposal) -> None:
    """A second revision = the operator worked on the proposal."""
    revision_service.add_revision(proposal, {"text": "mine"}, author_kind=RevisionAuthor.USER.value)
