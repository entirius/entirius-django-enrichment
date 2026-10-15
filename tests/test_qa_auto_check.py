# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Automatic checklist check of a run's text proposals: instance default, `params.qa_check` override, the check
time on the proposal (cleared by a new revision), its cost on the task, the settings API."""

from decimal import Decimal

import httpx
import pytest
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox
from pydantic import BaseModel

from django_enrichment.kinds import Kind, ProposalDraft, registry
from django_enrichment.models import ContentProposal
from django_enrichment.services import profile_service, proposal_service, qa_checklist, task_service
from tests.queue_helpers import run_queue

BASE = "/api/enrichment/v2/admin"
PROBLEM = {"message": "No benefit named.", "quote": ""}
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]


class _Options(BaseModel):
    pass


class _Params(BaseModel):
    channel: str = "default"


class _TextKind(Kind):
    """Writes one text without the toolbox, so the only completion is the check."""

    Options = _Options
    Params = _Params

    def run(self, worker, task, subject_ref):
        locator = {"channel": "default", "language": "pl", "feature_idx": "description"}
        return [ProposalDraft("attribute_value", locator, {"text": "A chair."}, subject_label="Chair")]


@pytest.fixture
def text_worker(make_worker, fake_adapter, monkeypatch):
    monkeypatch.setitem(registry._KINDS, "text", _TextKind)
    return make_worker(key="pim.text", kind="text", options={})


@pytest.fixture
def checklists():
    profile_service.save_profile(channel="", data={"checklists": {"text": "- Name the benefit."}})


def _answer(problems: list[dict], cost: str = "0.002") -> httpx.Response:
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": {"problems": problems}, "cost": cost})


def _run(capture, problems: list[dict] | None = None, **params):
    """Spawn and drain one run over SKU-1; returns (task, proposal, check calls)."""
    scope_spec = {"mode": "list", "module": "pim", "refs": ["SKU-1"]}
    task = task_service.spawn(type="pim.text", scope_spec=scope_spec, params=params)
    with mock_toolbox() as router, capture(execute=True):
        route = router["complete"].mock(return_value=_answer(problems or []))
        run_queue(task)
    task.refresh_from_db()  # the check ran after commit, after the queue drained
    return task, ContentProposal.objects.get(task=task), len(route.calls)


@pytest.mark.usefixtures("text_worker", "checklists")
class TestAutoCheck:
    def test_off_by_default_no_paid_call(self, django_capture_on_commit_callbacks):
        task, proposal, calls = _run(django_capture_on_commit_callbacks)
        assert calls == 0
        assert proposal.standard_checked_at is None
        assert "qa_check" not in task.params

    def test_instance_default_on_checks_and_charges_the_task(self, django_capture_on_commit_callbacks):
        profile_service.set_qa_check_default(True)
        task, proposal, calls = _run(django_capture_on_commit_callbacks, [PROBLEM])
        assert calls == 1
        assert proposal.standard_checked_at is not None
        assert [w["code"] for w in proposal.warnings] == ["checklist"]
        assert proposal.revisions.count() == 1
        assert task.cost_usd == Decimal("0.002")

    def test_run_off_overrides_the_default(self, django_capture_on_commit_callbacks):
        profile_service.set_qa_check_default(True)
        task, proposal, calls = _run(django_capture_on_commit_callbacks, qa_check=False)
        assert calls == 0 and proposal.standard_checked_at is None
        assert task.params["qa_check"] is False

    def test_run_on_overrides_the_default(self, django_capture_on_commit_callbacks):
        _, proposal, calls = _run(django_capture_on_commit_callbacks, qa_check=True)
        assert calls == 1 and proposal.standard_checked_at is not None
        assert proposal.warnings == []

    def test_a_new_revision_clears_the_check(self, django_capture_on_commit_callbacks, admin_user):
        _, proposal, _ = _run(django_capture_on_commit_callbacks, qa_check=True)
        proposal_service.update_value(proposal, {"text": "A comfortable chair."}, user=admin_user)
        proposal.refresh_from_db()
        assert proposal.standard_checked_at is None

    def test_without_checklists_the_proposal_stays_unchecked(self, django_capture_on_commit_callbacks):
        profile_service.save_profile(channel="", data={})
        _, proposal, calls = _run(django_capture_on_commit_callbacks, qa_check=True)
        assert calls == 0 and proposal.standard_checked_at is None

    def test_qa_check_must_be_a_boolean(self):
        with pytest.raises(ValueError, match="qa_check"):
            task_service.spawn(
                type="pim.text",
                scope_spec={"mode": "list", "module": "pim", "refs": ["SKU-1"]},
                params={"qa_check": "yes"},
            )


@pytest.mark.usefixtures("fake_adapter", "checklists")
class TestManualCheck:
    def _proposal(self):
        return proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator={"channel": "default", "language": "pl", "feature_idx": "description"},
            proposed_value={"text": "A chair."},
        )

    def test_check_standard_records_the_time_and_the_api_answers_it(self, admin_client):
        proposal = self._proposal()
        assert admin_client.get(f"{BASE}/proposals/{proposal.pk}/").json()["standard_checked_at"] is None
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_answer([]))
            body = admin_client.post(
                f"{BASE}/proposals/{proposal.pk}/revise/", {"quick_action": "check_standard"}, format="json"
            ).json()
        assert body["warnings"] == [] and body["standard_checked_at"]
        assert admin_client.get(f"{BASE}/proposals/{proposal.pk}/").json()["standard_checked_at"]

    def test_deterministic_recheck_clears_the_check(self, admin_client):
        proposal = self._proposal()
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_answer([PROBLEM]))
            qa_checklist.check_standard(proposal)
        assert admin_client.post(f"{BASE}/proposals/{proposal.pk}/check/").json()["standard_checked_at"] is None


class TestSettingsApi:
    def test_read_and_change_the_default(self, admin_client):
        url = f"{BASE}/profile/settings/"
        assert admin_client.get(url).json() == {"qa_check": False}
        assert admin_client.put(url, {"qa_check": True}, format="json").json() == {"qa_check": True}
        assert profile_service.qa_check_default() is True
        assert profile_service.get_profile("").version == 0  # a setting, not a profile version

    def test_refuses_a_non_boolean(self, admin_client):
        resp = admin_client.put(f"{BASE}/profile/settings/", {"qa_check": "yes"}, format="json")
        assert resp.status_code == 400
