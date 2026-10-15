# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Worker pull API: auth, 204, 409 LEASE_LOST, complete / fail, throttle; status and resume; task unit summary."""

import pytest

from django_enrichment.models import ContentProposal, Worker
from django_enrichment.services import unit_planner
from tests.test_runner import _spawn, echo_worker  # noqa: F401 — fixture

BASE = "/api/enrichment/v2/admin/workers/"
pytestmark = pytest.mark.django_db


@pytest.fixture
def task(echo_worker, settings):  # noqa: F811
    settings.ENRICHMENT_UNIT_SIZE = 2
    echo_worker(mode="external")
    task = _spawn(["SKU-1", "SKU-2", "SKU-3"])
    unit_planner.plan_task(task.pk)
    return task


def _claim(client, **over):
    return client.post(f"{BASE}claim/", {"worker": "pim.echo", "instance": "n8n-1", **over}, "json")


class TestAuth:
    @pytest.mark.parametrize("path", ["claim/", "heartbeat/", "complete/", "fail/", "status/"])
    def test_anonymous_is_refused(self, api_client, path):
        method = api_client.get if path == "status/" else api_client.post
        assert method(f"{BASE}{path}").status_code in (401, 403)

    @pytest.mark.parametrize("path", ["claim/", "heartbeat/", "complete/", "fail/", "status/"])
    def test_customers_are_refused(self, regular_client, path):
        method = regular_client.get if path == "status/" else regular_client.post
        assert method(f"{BASE}{path}").status_code == 403


class TestClaim:
    def test_claim_returns_a_unit_with_a_lease(self, admin_client, task):
        response = _claim(admin_client, version="1.0")
        body = response.json()
        assert response.status_code == 200
        assert body["subject_refs"] == ["SKU-1", "SKU-2"] and body["task_id"] == task.pk
        assert body["token"] and body["context"]["kind"] == "echo" and body["attempt"] == 1

    def test_nothing_to_do_is_204(self, admin_client, echo_worker):  # noqa: F811
        echo_worker(mode="external")
        assert _claim(admin_client).status_code == 204

    def test_unknown_worker_is_404_and_bad_body_400(self, admin_client, task):
        assert _claim(admin_client, worker="nope").status_code == 404
        assert admin_client.post(f"{BASE}claim/", {"worker": ""}, "json").status_code == 400

    def test_paused_worker_is_204(self, admin_client, task):
        Worker.objects.update(paused_reason="paused")
        assert _claim(admin_client).status_code == 204


class TestLifecycle:
    def test_heartbeat_complete_and_repeat(self, admin_client, task):
        token = _claim(admin_client).json()["token"]
        assert admin_client.post(f"{BASE}heartbeat/", {"token": token}, "json").status_code == 200
        results = [
            {
                "subject_ref": ref,
                "drafts": [
                    {
                        "target_kind": "attribute_value",
                        "target_locator": {"language": "pl"},
                        "proposed_value": {"text": ref},
                    }
                ],
            }
            for ref in ("SKU-1", "SKU-2")
        ]
        first = admin_client.post(f"{BASE}complete/", {"token": token, "results": results}, "json")
        again = admin_client.post(f"{BASE}complete/", {"token": token, "results": []}, "json")
        assert first.status_code == again.status_code == 200
        assert first.json() == again.json() == {"succeeded": 2, "failed": 0, "kept_edited": 0, "discarded": False}
        assert ContentProposal.objects.count() == 2

    def test_stale_token_is_409_lease_lost(self, admin_client, task):
        stale = "5b5f8f3e-0c1c-4a52-9a53-6a9b0a0b9a11"
        for path, body in (
            ("heartbeat/", {}),
            ("complete/", {"results": []}),
            ("fail/", {"code": "X", "message": "m"}),
        ):
            response = admin_client.post(f"{BASE}{path}", {"token": stale, **body}, "json")
            assert response.status_code == 409, path

    def test_foreign_subject_in_results_is_400(self, admin_client, task):
        token = _claim(admin_client).json()["token"]
        body = {"token": token, "results": [{"subject_ref": "SKU-9"}]}
        assert admin_client.post(f"{BASE}complete/", body, "json").status_code == 400

    def test_fail_requeues_with_a_backoff(self, admin_client, task):
        token = _claim(admin_client).json()["token"]
        response = admin_client.post(
            f"{BASE}fail/", {"token": token, "code": "TIMEOUT", "message": "Too slow."}, "json"
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued" and response.json()["not_before"]


class TestThrottle:
    def test_the_pull_api_is_throttled(self, admin_client, task, settings):
        from django_enrichment.api.admin.throttling import WorkerQueueThrottle

        WorkerQueueThrottle.rate = "2/min"
        try:
            codes = [_claim(admin_client, worker="nope").status_code for _ in range(4)]
        finally:
            WorkerQueueThrottle.rate = "300/min"
        assert codes[-1] == 429


class TestStatusAndTasks:
    def test_status_and_resume(self, admin_client, task):
        _claim(admin_client)
        rows = admin_client.get(f"{BASE}status/").json()
        row = next(r for r in rows if r["key"] == "pim.echo")
        assert (row["state"], row["queued_units"], row["leased_units"], row["online_instances"]) == ("running", 1, 1, 1)
        Worker.objects.update(paused_reason="Stopped for test.")
        assert admin_client.get(f"{BASE}status/").json()[0]["paused_reason"] == "Stopped for test."
        resumed = admin_client.post(f"{BASE}pim.echo/resume/")
        assert resumed.status_code == 200 and resumed.json()["state"] == "running"
        assert admin_client.post(f"{BASE}nope/resume/").status_code == 404

    def test_task_detail_has_unit_summary_and_failed_unit_trail(self, admin_client, task):
        token = _claim(admin_client).json()["token"]
        admin_client.post(
            f"{BASE}fail/", {"token": token, "code": "BAD", "message": "Bad.", "retryable": False}, "json"
        )
        body = admin_client.get(f"/api/enrichment/v2/admin/tasks/{task.pk}/").json()
        assert body["units"] == {"failed": 1, "queued": 1}
        failed = body["failed_units"][0]
        assert failed["error"] == {"code": "BAD", "message": "Bad."} and failed["events"][-1]["event"] == "failed"
