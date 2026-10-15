# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Run lanes (plan 22): routing per lane, size default, the run-now cap, in-flight dedupe, queue position."""

import pytest

from django_enrichment.enums import TaskStatus
from django_enrichment.models import EnrichmentTask
from django_enrichment.services import revise_batch_service, task_service, worker_service
from django_enrichment.tasks import revise_tasks, worker_runner

TASKS = "/api/enrichment/v2/admin/tasks/"
PARAMS = {"channel": "default", "fields": ["meta_title"], "languages": ["pl"]}
pytestmark = pytest.mark.django_db


@pytest.fixture
def writer(make_worker, fake_adapter):
    return make_worker(key="pim.writer", label="Writer", kind="write", options={})


@pytest.fixture
def queues(monkeypatch):
    """Celery queues the runner was sent to (on-commit runs at once, dispatch captured, nothing runs)."""
    sent: list[str] = []
    monkeypatch.setattr(worker_service.transaction, "on_commit", lambda callback: callback())
    monkeypatch.setattr(worker_runner.run_task, "apply_async", lambda args, queue: sent.append(queue))
    return sent


def _scope(*refs: str) -> dict:
    return {"mode": "list", "module": "pim", "refs": list(refs)}


def _spawn(*refs: str, lane: str = "", **params) -> EnrichmentTask:
    return task_service.spawn(type="pim.writer", scope_spec=_scope(*refs), params={**PARAMS, **params}, lane=lane)


def _post(client, *refs: str, **body):
    return client.post(TASKS, {"type": "pim.writer", "scope_spec": _scope(*refs), "params": PARAMS, **body}, "json")


class TestRouting:
    def test_one_subject_runs_now_on_the_interactive_queue(self, writer, queues):
        assert _spawn("SKU-1").lane == "now"
        assert queues == ["enrichment_interactive"]

    def test_more_subjects_go_to_the_bulk_queue(self, writer, queues):
        assert _spawn("SKU-1", "SKU-2", "SKU-3").lane == "queue"
        assert queues == ["enrichment_bulk"]

    def test_explicit_lane_wins_over_size_and_queue_names_come_from_settings(self, writer, queues, settings):
        settings.ENRICHMENT_BULK_QUEUE = "slow"
        assert _spawn("SKU-1", lane="queue").lane == "queue"
        assert _spawn("SKU-1", "SKU-2", lane="now").lane == "now"
        assert queues == ["slow", "enrichment_interactive"]

    def test_non_list_scope_defaults_to_queue(self, writer, queues):
        task = task_service.spawn(type="pim.writer", scope_spec={"mode": "filter", "module": "pim"}, params=PARAMS)
        assert task.lane == "queue"

    def test_batch_correction_uses_the_interactive_queue(self, monkeypatch, fake_adapter, admin_user):
        sent = []
        monkeypatch.setattr(revise_tasks.revise_task, "apply_async", lambda args, queue: sent.append(queue))
        monkeypatch.setattr(revise_batch_service, "_pending", lambda task: EnrichmentTask.objects.all())
        revise_batch_service.queue(EnrichmentTask.objects.create(type="pim.writer"), {"instruction": "Fix."})
        assert sent == ["enrichment_interactive"]


class TestRunNowCap:
    def test_over_the_cap_is_a_plain_400(self, admin_client, writer, settings):
        settings.ENRICHMENT_RUN_NOW_MAX_SUBJECTS = 2
        response = _post(admin_client, "SKU-1", "SKU-2", "SKU-3", lane="now")
        assert response.status_code == 400
        assert "Run now takes at most 2 items" in str(response.json())
        assert not EnrichmentTask.objects.exists()

    def test_run_now_needs_a_list_scope(self, writer):
        with pytest.raises(ValueError, match="Add this run to the queue"):
            task_service.spawn(
                type="pim.writer", scope_spec={"mode": "filter", "module": "pim"}, params=PARAMS, lane="now"
            )

    def test_unknown_lane_is_rejected_by_the_schema(self, admin_client, writer):
        assert _post(admin_client, "SKU-1", lane="later").status_code == 400


class TestInFlightDedupe:
    def test_partial_overlap_skips_the_running_subjects(self, admin_client, writer):
        _spawn("SKU-1")
        response = _post(admin_client, "SKU-1", "SKU-2")
        body = response.json()
        assert response.status_code == 201, body
        assert body["skipped_in_flight"] == ["SKU-1"]
        assert body["scope_spec"]["refs"] == ["SKU-2"]
        assert (body["lane"], body["queued_ahead"]) == ("now", 1)  # SKU-1's run is ahead on the same lane

    def test_all_running_is_a_409(self, admin_client, writer):
        task_service.mark_in_progress(_spawn("SKU-1", "SKU-2"))
        response = _post(admin_client, "SKU-2", "SKU-1")
        assert response.status_code == 409
        assert response.json()["error"] == "ALREADY_RUNNING"
        assert response.json()["message"] == "This run is already in progress for all 2 items. Wait for it to finish."
        assert EnrichmentTask.objects.count() == 1

    @pytest.mark.parametrize("change", [{"languages": ["en"]}, {"fields": ["meta_description"]}, {"channel": "other"}])
    def test_a_different_run_is_not_skipped(self, admin_client, writer, change):
        _spawn("SKU-1", **change)
        response = _post(admin_client, "SKU-1")
        assert (response.status_code, response.json()["skipped_in_flight"]) == (201, [])

    @pytest.mark.parametrize("status", [TaskStatus.DONE.value, TaskStatus.CANCELLED.value])
    def test_finished_runs_do_not_block(self, admin_client, writer, status):
        EnrichmentTask.objects.filter(pk=_spawn("SKU-1").pk).update(status=status)
        assert _post(admin_client, "SKU-1").status_code == 201

    def test_language_order_does_not_matter(self, admin_client, writer):
        _spawn("SKU-1", languages=["pl", "en"])
        body = {"params": {**PARAMS, "languages": ["en", "pl"]}}
        assert _post(admin_client, "SKU-1", **body).status_code == 409


class TestQueuedAhead:
    def test_counts_earlier_open_runs_of_the_same_lane(self, admin_client, writer):
        first, second = _spawn("A", "B"), _spawn("C", "D")
        now = _spawn("E")
        assert admin_client.get(f"{TASKS}{second.pk}/").json()["queued_ahead"] == 1
        assert admin_client.get(f"{TASKS}{now.pk}/").json()["queued_ahead"] == 0
        task_service.mark_in_progress(first)
        rows = {row["id"]: row["queued_ahead"] for row in admin_client.get(TASKS).json()["results"]}
        assert rows == {first.pk: None, second.pk: 0, now.pk: 0}

    def test_external_worker_tasks_have_no_position(self, admin_client, make_worker, fake_adapter):
        make_worker(key="pim.writer", label="Writer", kind="write", options={}, mode="external")
        assert admin_client.get(f"{TASKS}{_spawn('SKU-1').pk}/").json()["queued_ahead"] is None
