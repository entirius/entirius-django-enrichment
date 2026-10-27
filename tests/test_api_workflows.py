# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""API: `workflows/`, `runs/` (start, retrieve, regenerate, cancel), auth, and `ANSWERS_MISSING` on accept."""

import pytest
from django_utils.toolbox.testing import mock_toolbox

from django_enrichment.models import ContentProposal
from tests import workflow_setup as wf

BASE = "/api/enrichment/v2/admin"
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]


@pytest.fixture
def article(fake_adapter, settings):
    settings.ENRICHMENT_ADAPTERS = {"pim": "tests.fake_adapter", wf.DOC_MODULE: "tests.fake_adapter"}
    wf.make_templates()
    steps = [wf.step("ask", output="questions"), wf.step("draft", needs=("ask",))]
    return wf.make_definition("article", steps, {"type": "object", "required": ["topic"]})


@pytest.fixture
def created(article, admin_client, django_capture_on_commit_callbacks):
    """POST runs/ (the answer is sent before the first step's task runs), then that task runs on a mocked toolbox."""
    with mock_toolbox() as router, django_capture_on_commit_callbacks(execute=True):
        router["complete"].mock(side_effect=wf.respond)
        response = admin_client.post(
            f"{BASE}/runs/", {"definition": "article", "inputs": {"topic": "Sofa"}}, format="json"
        )
    assert response.status_code == 201, response.content
    return response.json()


@pytest.fixture
def started(created, admin_client):
    """The run once its first step waits for review."""
    return admin_client.get(f"{BASE}/runs/{created['uid']}/").json()


class TestAuth:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/workflows/"),
            ("get", "/runs/"),
            ("post", "/runs/"),
            ("get", "/runs/abc/"),
            ("post", "/runs/abc/steps/ask/regenerate/"),
            ("post", "/runs/abc/cancel/"),
        ],
    )
    def test_anonymous_401_and_non_admin_403(self, api_client, regular_user, method, path):
        assert getattr(api_client, method)(f"{BASE}{path}").status_code == 401
        api_client.force_authenticate(user=regular_user)
        assert getattr(api_client, method)(f"{BASE}{path}").status_code == 403


class TestWorkflows:
    def test_lists_active_definitions_with_steps(self, article, admin_client):
        body = admin_client.get(f"{BASE}/workflows/").json()
        assert [item["key"] for item in body] == ["article"]
        assert body[0]["steps"][1] == {
            "key": "draft",
            "label": "Draft",
            "template": "draft",
            "needs": ["ask"],
            "gate": "review",
            "output": "artifact",
        }


class TestRuns:
    def test_start_answers_while_the_first_step_runs(self, created):
        assert (created["status"], [step["status"] for step in created["steps"]]) == ("running", ["running", "waiting"])

    def test_the_run_waits_once_the_step_is_done(self, started):
        assert (started["definition"], started["status"], started["current_step"]) == ("article", "waiting", "ask")
        assert [step["status"] for step in started["steps"]] == ["review", "waiting"]
        assert started["cost_usd"] == "0.01000000" and started["cost_complete"] is True

    def test_start_validation_errors_are_400(self, article, admin_client):
        bad_inputs = admin_client.post(f"{BASE}/runs/", {"definition": "article", "inputs": {}}, format="json")
        unknown = admin_client.post(f"{BASE}/runs/", {"definition": "nope"}, format="json")
        assert (bad_inputs.status_code, unknown.status_code) == (400, 400)

    def test_list_and_retrieve(self, started, admin_client):
        listed = admin_client.get(f"{BASE}/runs/", {"status": "waiting"}).json()
        assert [run["uid"] for run in listed["results"]] == [started["uid"]]
        assert admin_client.get(f"{BASE}/runs/{started['uid']}/").json()["uid"] == started["uid"]
        assert admin_client.get(f"{BASE}/runs/nope/").status_code == 404

    def test_regenerate(self, started, admin_client, django_capture_on_commit_callbacks):
        url = f"{BASE}/runs/{started['uid']}/steps/ask/regenerate/"
        with mock_toolbox() as router, django_capture_on_commit_callbacks(execute=True):
            router["complete"].mock(side_effect=wf.respond)
            response = admin_client.post(url, {"instruction": "Fewer questions."}, format="json")
        assert response.status_code == 200
        assert admin_client.post(f"{BASE}/runs/{started['uid']}/steps/draft/regenerate/").status_code == 400
        assert admin_client.post(f"{BASE}/runs/{started['uid']}/steps/nope/regenerate/").status_code == 404

    def test_cancel_once(self, started, admin_client):
        url = f"{BASE}/runs/{started['uid']}/cancel/"
        assert admin_client.post(url).json()["status"] == "cancelled"
        assert admin_client.post(url).status_code == 400


class TestApproveQuestions:
    def test_without_a_minimum_the_required_answers_are_missing(self, started, admin_client):
        proposal_id = started["steps"][0]["proposal_id"]
        value = admin_client.get(f"{BASE}/proposals/{proposal_id}/").json()["proposed_value"]
        admin_client.patch(
            f"{BASE}/proposals/{proposal_id}/", {"proposed_value": {**value, "min_answers": 0}}, format="json"
        )
        response = admin_client.post(f"{BASE}/proposals/{proposal_id}/accept/")
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "ANSWERS_MISSING"
        assert [detail["field"] for detail in body["details"]] == ["years", "tone"]
        assert {detail["issue"] for detail in body["details"]} == {"REQUIRED"}
        assert ContentProposal.objects.get(pk=proposal_id).status == "pending"

    def test_too_few_answers_name_the_minimum(self, started, admin_client):
        proposal_id = started["steps"][0]["proposal_id"]
        body = admin_client.post(f"{BASE}/proposals/{proposal_id}/accept/").json()
        assert body["error"] == "ANSWERS_MISSING"
        assert body["message"] == "Answer at least 1 question before approving."
        assert {(d["field"], d["issue"]) for d in body["details"]} == {
            ("years", "MIN_ANSWERS"),
            ("tone", "MIN_ANSWERS"),
            ("extra", "MIN_ANSWERS"),
        }
        assert {d["description"] for d in body["details"]} == {"Answer at least 1 of these questions."}

    def test_answered_questions_approve_the_step(self, started, admin_client, django_capture_on_commit_callbacks):
        proposal_id = started["steps"][0]["proposal_id"]
        value = admin_client.get(f"{BASE}/proposals/{proposal_id}/").json()["proposed_value"]
        value["answers"] = {"years": "1999", "tone": "warm"}
        admin_client.patch(f"{BASE}/proposals/{proposal_id}/", {"proposed_value": value}, format="json")
        with mock_toolbox() as router, django_capture_on_commit_callbacks(execute=True):
            router["complete"].mock(side_effect=wf.respond)
            accepted = admin_client.post(f"{BASE}/proposals/{proposal_id}/accept/")
        assert (accepted.status_code, accepted.json()["result_ref"]) == (200, "")
        run = admin_client.get(f"{BASE}/runs/{started['uid']}/").json()
        assert [step["status"] for step in run["steps"]] == ["approved", "review"]
