# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — revisions, corrections, rules, batch correction, `proposals/?task=` (contract A3 § 1)."""

import httpx
import pytest
from django_utils.toolbox.testing import CANNED_COMPLETION, error_response, mock_toolbox

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import EnrichmentTask
from django_enrichment.services import proposal_service

BASE = "/api/enrichment/v2/admin"
LOCATOR = {"channel": "default", "language": "pl", "feature_idx": "description"}
LONG = "Solid steel body. " * 20 + "The warranty lasts one year."
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]


def _answer(parsed: dict) -> httpx.Response:
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": parsed})


def _proposal(value: dict | None = None, **over):
    fields = {
        "target_module": "pim",
        "subject_ref": "SKU-1",
        "target_kind": "attribute_value",
        "target_locator": LOCATOR,
        "proposed_value": value or {"text": "Copy."},
    }
    return proposal_service.intake(**{**fields, **over})


def _revise(client, proposal, body: dict, answer: httpx.Response):
    with mock_toolbox() as router:
        router["complete"].mock(return_value=answer)
        return client.post(f"{BASE}/proposals/{proposal.pk}/revise/", body, format="json")


class TestAuth:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "proposals/1/revisions/"),
            ("post", "proposals/1/revise/"),
            ("post", "proposals/1/revisions/1/restore/"),
            ("put", "proposals/1/rules/"),
            ("post", "proposals/1/rules/promote/"),
            ("post", "tasks/1/revise/"),
        ],
    )
    def test_anonymous_401_and_non_admin_403(self, api_client, regular_user, method, path):
        assert getattr(api_client, method)(f"{BASE}/{path}", {}, format="json").status_code == 401
        api_client.force_authenticate(user=regular_user)
        assert getattr(api_client, method)(f"{BASE}/{path}", {}, format="json").status_code == 403


class TestRevisions:
    def test_list_after_intake_and_patch(self, admin_client, admin_user, fake_adapter):
        proposal = _proposal()
        admin_client.patch(f"{BASE}/proposals/{proposal.pk}/", {"proposed_value": {"text": "Mine."}}, format="json")
        resp = admin_client.get(f"{BASE}/proposals/{proposal.pk}/revisions/")
        assert resp.status_code == 200
        rows = resp.json()
        assert [(r["number"], r["author_kind"], r["author"]) for r in rows] == [
            (1, "ai", None),
            (2, "user", admin_user.pk),
        ]
        assert set(rows[0]) == {
            "number",
            "value",
            "instruction",
            "scope",
            "author_kind",
            "author",
            "cost_usd",
            "profile_versions",
            "warnings",
            "created_at",
        }

    def test_unknown_proposal_404(self, admin_client):
        assert admin_client.get(f"{BASE}/proposals/999/revisions/").status_code == 404

    def test_restore(self, admin_client, fake_adapter):
        proposal = _proposal()
        proposal_service.update_value(proposal, {"text": "Two."})
        resp = admin_client.post(f"{BASE}/proposals/{proposal.pk}/revisions/1/restore/")
        assert resp.status_code == 201
        assert (resp.json()["number"], resp.json()["author_kind"], resp.json()["value"]) == (
            3,
            "restore",
            {"text": "Copy."},
        )
        assert admin_client.post(f"{BASE}/proposals/{proposal.pk}/revisions/9/restore/").status_code == 404


class TestRevise:
    def test_correction_201(self, admin_client, fake_adapter):
        resp = _revise(admin_client, _proposal(), {"quick_action": "shorten"}, _answer({"text": "Short."}))
        assert resp.status_code == 201
        assert (resp.json()["number"], resp.json()["value"]) == (2, {"text": "Short."})

    def test_variants_200(self, admin_client, fake_adapter):
        resp = _revise(admin_client, _proposal(), {"quick_action": "variants"}, _answer({"candidates": ["A", "B"]}))
        assert resp.status_code == 200
        assert resp.json() == {"candidates": [{"text": "A"}, {"text": "B"}]}

    def test_not_applicable_422(self, admin_client, fake_adapter):
        answer = _answer({"edits": [{"old": "missing", "new": "x"}], "rewrite": None})
        resp = _revise(admin_client, _proposal({"text": LONG}), {"instruction": "Fix."}, answer)
        assert resp.status_code == 422
        assert resp.json()["error"] == "REVISION_NOT_APPLICABLE"

    @pytest.mark.parametrize(
        "body",
        [
            {"quick_action": "poem"},
            {"instruction": ""},
            {"instruction": "x", "scope": {"type": "selection", "text": "absent"}},
        ],
    )
    def test_invalid_input_400(self, admin_client, fake_adapter, body):
        resp = _revise(admin_client, _proposal(), body, _answer({"text": "x"}))
        assert resp.status_code == 400
        assert resp.json()["error"] == "VALIDATION_ERROR"

    def test_not_revisable_400(self, admin_client, fake_adapter):
        proposal = _proposal()
        proposal.status = ProposalStatus.APPLIED.value
        proposal.save()
        resp = _revise(admin_client, proposal, {"quick_action": "shorten"}, _answer({"text": "x"}))
        assert resp.status_code == 400

    def test_toolbox_error_mapped(self, admin_client, fake_adapter):
        resp = _revise(admin_client, _proposal(), {"quick_action": "shorten"}, error_response(402, "BUDGET_EXCEEDED"))
        assert resp.status_code == 402
        assert resp.json()["error"] == "BUDGET_EXCEEDED"


class TestRules:
    def test_put_rules(self, admin_client, fake_adapter):
        proposal = _proposal()
        resp = admin_client.put(f"{BASE}/proposals/{proposal.pk}/rules/", {"rules": ["No emojis."]}, format="json")
        assert resp.status_code == 200
        assert resp.json()["rules"] == ["No emojis."]
        too_many = {"rules": [f"r{i}" for i in range(21)]}
        assert admin_client.put(f"{BASE}/proposals/{proposal.pk}/rules/", too_many, format="json").status_code == 400

    def test_promote(self, admin_client, fake_adapter, make_worker):
        make_worker(key="pim.writer", kind="field", options={"field": "description"})
        proposal = _proposal(source="worker:pim.writer@v1")
        url = f"{BASE}/proposals/{proposal.pk}/rules/promote/"
        resp = admin_client.post(url, {"rule": "No emojis.", "target": "worker"}, format="json")
        assert resp.status_code == 200
        assert resp.json() == {"target": "worker", "rules": ["No emojis."]}
        assert admin_client.post(url, {"rule": "x", "target": "template"}, format="json").status_code == 400


class TestTaskRevise:
    def test_batch_202_and_task_filter(self, admin_client, fake_adapter):
        task = EnrichmentTask.objects.create(type="pim.writer")
        other = _proposal(subject_ref="SKU-9")
        mine = _proposal(task=task)
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_answer({"text": "Fixed."}))
            resp = admin_client.post(f"{BASE}/tasks/{task.pk}/revise/", {"instruction": "Fix."}, format="json")
        assert (resp.status_code, resp.json()) == (202, {"queued": 1})
        listed = admin_client.get(f"{BASE}/proposals/", {"task": task.pk}).json()["results"]
        assert [(p["id"], p["proposed_value"]) for p in listed] == [(mine.pk, {"text": "Fixed."})]
        assert other.pk not in [p["id"] for p in listed]

    def test_bad_task_filter_400(self, admin_client):
        assert admin_client.get(f"{BASE}/proposals/", {"task": "abc"}).status_code == 400

    def test_variants_batch_400(self, admin_client, fake_adapter):
        task = EnrichmentTask.objects.create(type="pim.writer")
        resp = admin_client.post(f"{BASE}/tasks/{task.pk}/revise/", {"quick_action": "variants"}, format="json")
        assert resp.status_code == 400
