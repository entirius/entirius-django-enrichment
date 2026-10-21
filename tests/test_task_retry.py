# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retry of a finished task (`POST tasks/{id}/retry/`): only the failed subjects, the same params minus the keys the
bus wrote while the task ran, `params.retry_of`, the refusals and the spawn path shared with `POST tasks/`."""

import pytest

from django_enrichment.enums import TaskStatus
from django_enrichment.kinds import ProposalDraft
from django_enrichment.models import EnrichmentTask, WorkUnit
from django_enrichment.services import (
    queue_dispatch,
    retry_service,
    run_service,
    task_service,
    template_service,
    unit_planner,
    unit_result_service,
    work_queue_service,
)
from django_enrichment.services.queue_types import SubjectResult
from django_enrichment.tasks import worker_runner

TASKS = "/api/enrichment/v2/admin/tasks/"
PARAMS = {"channel": "default", "fields": ["meta_title"], "languages": ["pl"]}
DONE, FAILED = TaskStatus.DONE.value, TaskStatus.FAILED.value
pytestmark = pytest.mark.django_db


@pytest.fixture
def writer(make_worker, fake_adapter):
    """External worker: units are planned but nothing consumes them."""
    return make_worker(key="pim.writer", label="Writer", kind="write", options={}, mode="external")


@pytest.fixture
def queues(monkeypatch):
    """Celery queues the planner was sent to (on-commit runs at once, dispatch captured, nothing runs)."""
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(queue_dispatch.transaction, "on_commit", lambda callback: callback())
    monkeypatch.setattr(worker_runner.plan_task, "apply_async", lambda args, queue: sent.append((args[0], queue)))
    return sent


def _scope(*refs: str, **extra) -> dict:
    return {"mode": "list", "module": "pim", "refs": list(refs), **extra}


def _spawn(*refs: str, **over) -> EnrichmentTask:
    over = {"type": "pim.writer", "scope_spec": _scope(*refs), "params": PARAMS, **over}
    return task_service.spawn(**over)


def _finish(task: EnrichmentTask, *failed: str, status: str = DONE, **fields) -> EnrichmentTask:
    """Close `task` the way the queue does: a status and one error per failed subject."""
    errors = [{"subject_ref": ref, "code": "TIMEOUT", "error_id": f"e{n}"} for n, ref in enumerate(failed)]
    EnrichmentTask.objects.filter(pk=task.pk).update(**{"status": status, "errors": errors, **fields})
    task.refresh_from_db()
    return task


def _ok(ref: str) -> SubjectResult:
    return SubjectResult(ref, [ProposalDraft("attribute_value", {"language": "pl"}, {"text": ref}, subject_label=ref)])


def _retry(client, task: EnrichmentTask):
    return client.post(f"{TASKS}{task.pk}/retry/")


class TestFailedSubjects:
    def test_partial_failure_retries_only_the_failed_subjects(self, admin_client, admin_user, writer):
        task = _finish(_spawn("SKU-1", "SKU-2", "SKU-3", "SKU-4"), "SKU-2", "SKU-4", "SKU-2")
        response = _retry(admin_client, task)
        body = response.json()
        assert response.status_code == 201, body
        assert body["scope_spec"] == _scope("SKU-2", "SKU-4")
        assert (body["type"], body["status"], body["lane"]) == ("pim.writer", "open", "queue")
        assert (body["requested_by_id"], body["batch_key"], body["skipped_in_flight"]) == (admin_user.pk, "", [])
        assert body["id"] != task.pk

    def test_full_failure_retries_every_failed_subject(self, admin_client, writer):
        task = _finish(_spawn("SKU-1", "SKU-2"), "SKU-1", "SKU-2", status=FAILED)
        assert _retry(admin_client, task).json()["scope_spec"]["refs"] == ["SKU-1", "SKU-2"]

    def test_a_list_scope_keeps_its_other_keys(self, admin_client, writer):
        task = _finish(_spawn("SKU-1", "SKU-2", scope_spec=_scope("SKU-1", "SKU-2", channel="default")), "SKU-2")
        assert _retry(admin_client, task).json()["scope_spec"] == _scope("SKU-2", channel="default")

    @pytest.mark.parametrize(
        "scope_spec",
        [
            {"mode": "filter", "module": "pim", "channel": "default", "filters": {"q": "sofa"}},
            {"mode": "gap", "module": "pim", "channel": "default", "check": "missing", "scope": {"candidates": ["A"]}},
            {"mode": "csv", "module": "pim", "channel": "default", "file": "f.csv", "row_type": "pim.writer"},
        ],
    )
    def test_another_scope_is_retried_as_a_list_of_its_module(self, admin_client, writer, scope_spec):
        task = _finish(_spawn(scope_spec=scope_spec), "SKU-7")
        assert _retry(admin_client, task).json()["scope_spec"] == _scope("SKU-7", channel="default")

    def test_a_failed_task_naming_no_subject_retries_its_whole_scope(self, admin_client, writer):
        scope_spec = {"mode": "filter", "module": "pim", "channel": "default", "filters": {"q": "sofa"}}
        task = _finish(_spawn(scope_spec=scope_spec), status=FAILED)
        assert _retry(admin_client, task).json()["scope_spec"] == scope_spec

    def test_a_run_level_error_alone_names_no_subject(self, admin_client, writer):
        task = _spawn("SKU-1", "SKU-2")
        _finish(task, status=FAILED, errors=[{"subject_ref": "", "code": "COST_LIMIT", "error_id": "-"}])
        assert _retry(admin_client, task).json()["scope_spec"]["refs"] == ["SKU-1", "SKU-2"]

    def test_a_cancelled_task_retries_what_failed_before_the_cancel(self, admin_client, writer):
        task = _finish(_spawn("SKU-1", "SKU-2"), "SKU-1", status=TaskStatus.CANCELLED.value)
        assert _retry(admin_client, task).json()["scope_spec"]["refs"] == ["SKU-1"]


class TestParams:
    def test_params_are_copied_without_the_keys_written_while_it_ran(self, admin_client):
        task = _spawn("SKU-1", type="fix-attribute", params={"feature": "description", "tone": "dry"})
        task.params = {**task.params, "context_numbers": {"SKU-1": ["12"]}, "retry_of": 3}
        task.save(update_fields=["params"])
        body = _retry(admin_client, _finish(task, "SKU-1")).json()
        assert body["params"] == {"feature": "description", "tone": "dry", "retry_of": task.pk}

    def test_a_worker_run_keeps_its_params_and_marks_the_retry(self, admin_client, writer):
        task = _spawn("SKU-1", params={**PARAMS, "prompt": "Mention the warranty.", "qa_check": False})
        task.params = {**task.params, "context_numbers": {"SKU-1": ["12"]}}
        task.save(update_fields=["params"])
        params = _retry(admin_client, _finish(task, "SKU-1")).json()["params"]
        assert params == {**{k: v for k, v in task.params.items() if k != "context_numbers"}, "retry_of": task.pk}
        assert params["prompt"] == "Mention the warranty." and params["qa_check"] is False

    def test_the_template_and_its_rules_carry_over(self, admin_client, writer):
        template_service.create_template(
            key="meta", label="Meta", worker_key="pim.writer", params=PARAMS, rules=["No prices."]
        )
        task = _finish(_spawn("SKU-1", type="", template="meta", params={}), "SKU-1")
        params = _retry(admin_client, task).json()["params"]
        assert (params["template"], params["rules"], params["retry_of"]) == ("meta", ["No prices."], task.pk)

    def test_a_retry_of_a_retry_points_at_the_retried_task(self, admin_client, writer):
        first = _finish(_spawn("SKU-1", "SKU-2"), "SKU-1")
        second = EnrichmentTask.objects.get(pk=_retry(admin_client, first).json()["id"])
        third = _retry(admin_client, _finish(second, "SKU-1")).json()
        assert (second.params["retry_of"], third["params"]["retry_of"]) == (first.pk, second.pk)


class TestAccess:
    def test_401_without_auth(self, api_client, writer):
        task = _finish(_spawn("SKU-1"), "SKU-1")
        assert _retry(api_client, task).status_code == 401

    def test_403_for_a_non_admin(self, regular_client, writer):
        task = _finish(_spawn("SKU-1"), "SKU-1")
        assert _retry(regular_client, task).status_code == 403
        assert EnrichmentTask.objects.count() == 1


class TestRefusals:
    @pytest.mark.parametrize("status", [TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value])
    def test_a_running_task_is_a_409(self, admin_client, writer, status):
        task = _spawn("SKU-1")
        _finish(task, "SKU-1", status=status)
        response = _retry(admin_client, task)
        assert (response.status_code, response.json()["error"]) == (409, "NOT_RETRYABLE")
        assert "still in progress" in response.json()["message"]
        assert EnrichmentTask.objects.count() == 1

    @pytest.mark.parametrize("status", [DONE, TaskStatus.CANCELLED.value])
    def test_a_task_without_a_failed_subject_is_a_409(self, admin_client, writer, status):
        task = _finish(_spawn("SKU-1"), status=status)
        response = _retry(admin_client, task)
        assert (response.status_code, response.json()["error"]) == (409, "NOT_RETRYABLE")
        assert EnrichmentTask.objects.count() == 1

    def test_a_done_task_with_only_a_run_level_error_is_a_409(self, admin_client, writer):
        task = _spawn("SKU-1")
        _finish(task, errors=[{"subject_ref": "", "code": "COST_LIMIT", "error_id": "-"}])
        assert _retry(admin_client, task).status_code == 409

    def test_a_workflow_step_is_a_400(self, admin_client, writer):
        task = _finish(_spawn("run:abc"), "run:abc", status=FAILED, params={**PARAMS, "run": "abc", "step": "outline"})
        response = _retry(admin_client, task)
        assert (response.status_code, response.json()["error"]) == (400, "VALIDATION_ERROR")
        assert EnrichmentTask.objects.count() == 1

    def test_a_task_on_a_run_subject_is_a_400_even_without_the_run_param(self, admin_client, writer):
        task = _finish(_spawn("run:abc"), "run:abc", status=FAILED)
        assert _retry(admin_client, task).status_code == 400

    def test_an_unknown_task_is_a_404(self, admin_client, writer):
        response = admin_client.post(f"{TASKS}999/retry/")
        assert (response.status_code, response.json()["error"]) == (404, "NOT_FOUND")

    def test_the_same_retry_twice_is_already_running(self, admin_client, writer):
        task = _finish(_spawn("SKU-1", "SKU-2"), "SKU-1", "SKU-2")
        assert _retry(admin_client, task).status_code == 201
        response = _retry(admin_client, task)
        assert (response.status_code, response.json()["error"]) == (409, "ALREADY_RUNNING")
        assert EnrichmentTask.objects.count() == 2

    def test_a_subject_already_in_flight_is_skipped(self, admin_client, writer):
        task = _finish(_spawn("SKU-1", "SKU-2", "SKU-3"), "SKU-1", "SKU-2")
        _spawn("SKU-1")
        body = _retry(admin_client, task).json()
        assert (body["skipped_in_flight"], body["scope_spec"]["refs"]) == (["SKU-1"], ["SKU-2"])

    def test_the_spawn_limits_apply_as_for_a_new_run(self, admin_client, writer, settings):
        task = _finish(_spawn("SKU-1", "SKU-2"), "SKU-1", "SKU-2")
        settings.ENRICHMENT_QUEUE_MAX_SUBJECTS = 1
        response = _retry(admin_client, task)
        assert response.status_code == 400
        assert "A run takes at most 1 items" in str(response.json())
        assert EnrichmentTask.objects.count() == 1

    def test_the_service_raises_typed_errors(self, writer):
        with pytest.raises(retry_service.NotRetryableError, match="still in progress"):
            retry_service.retry(_spawn("SKU-1"))
        with pytest.raises(retry_service.NotRetryableError, match="nothing to retry"):
            retry_service.retry(_finish(_spawn("SKU-2")))
        failed = _finish(_spawn("SKU-3"), "SKU-3")
        retry_service.retry(failed)
        with pytest.raises(run_service.AlreadyRunningError):
            retry_service.retry(failed)


class TestDispatch:
    def test_a_single_subject_retry_runs_now_on_the_interactive_queue(self, admin_client, writer, queues):
        task = _finish(_spawn("SKU-1", "SKU-2"), "SKU-2")
        body = _retry(admin_client, task).json()
        assert (body["lane"], queues[-1]) == ("now", (body["id"], "enrichment_interactive"))

    def test_the_retry_is_planned_into_units_of_the_failed_subjects(
        self, admin_client, writer, settings, django_capture_on_commit_callbacks
    ):
        settings.ENRICHMENT_UNIT_SIZE = 2
        task = _spawn("SKU-1", "SKU-2", "SKU-3")
        unit_planner.plan_task(task.pk)
        first = work_queue_service.claim("pim.writer", "w1")  # SKU-1 ok, SKU-2 fails, SKU-3 never answered
        unit_result_service.complete(first.lease_token, [_ok("SKU-1"), SubjectResult("SKU-2", error_code="TIMEOUT")])
        second = work_queue_service.claim("pim.writer", "w1")
        unit_result_service.complete(second.lease_token, [])
        task.refresh_from_db()
        assert (task.status, [e["subject_ref"] for e in task.errors]) == (DONE, ["SKU-2", "SKU-3"])

        with django_capture_on_commit_callbacks(execute=True):
            retry_id = _retry(admin_client, task).json()["id"]
        units = WorkUnit.objects.filter(task_id=retry_id).order_by("pk")
        assert [unit.subject_refs for unit in units] == [["SKU-2", "SKU-3"]]
