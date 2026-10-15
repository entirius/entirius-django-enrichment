# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Cost ledger (contract A4 § 1): toolbox costs post factum on tasks, proposals and revisions — mocked toolbox."""

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx
from django_utils.toolbox import ToolboxError
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.kinds.base import ProposalDraft, with_cost
from django_enrichment.kinds.translate import TranslateKind
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import cost_service, proposal_service, task_service, toolbox_calls

BASE = "/api/enrichment/v2/admin"
TRANSLATE_URL = "http://toolbox.test/api/ai-translator/v2/admin/shop/translate/"
CATALOG = [
    {"key": "meta_title", "label": "Meta title", "type": "text", "max_length": 60, "translatable": True},
    {"key": "meta_description", "label": "Meta description", "type": "text", "max_length": 160, "translatable": True},
]
_PARSED = {"meta_title": "Chair", "meta_description": "A chair."}
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]


def _answer(cost: str | None) -> httpx.Response:
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": _PARSED, "cost": cost})


def _run_writer(make_worker, capture, cost: str | None, *, params: dict | None = None) -> tuple[EnrichmentTask, list]:
    make_worker(key="pim.writer", label="Writer", kind="write", options={})
    with mock_toolbox() as router, capture(execute=True):
        route = router["complete"].mock(return_value=_answer(cost))
        task = task_service.spawn(
            type="pim.writer",
            scope_spec={"mode": "list", "module": "pim", "refs": ["SKU-1"]},
            params={"channel": "default", "fields": ["meta_title", "meta_description"], "languages": ["pl"]}
            | (params or {}),
        )
    task.refresh_from_db()
    return task, [json.loads(call.request.content) for call in route.calls]


@pytest.fixture
def catalog(fake_adapter):
    fake_adapter.set_catalog(CATALOG)
    return fake_adapter


class TestCostOf:
    @pytest.mark.parametrize(
        ("source", "body", "expected"),
        [
            ("completion", {"cost": "0.000022"}, Decimal("0.000022")),
            ("translate", {"total_cost_usd": 0.5}, Decimal("0.5")),
            ("media", {"cost": "1.20"}, Decimal("1.20")),
            ("seo", {"cost": "0"}, Decimal("0")),
            ("completion", {}, None),
            ("translate", {"cost": "1"}, None),
            ("completion", {"cost": "n/a"}, None),
            ("completion", {"cost": "NaN"}, None),
            ("completion", {"cost": "Infinity"}, None),
            ("completion", {"cost": "-1"}, None),
            ("completion", {"cost": "1e20"}, None),
            ("completion", {"cost": "0.123456789"}, Decimal("0.12345679")),
        ],
    )
    def test_one_mapping_per_source(self, source, body, expected):
        assert toolbox_calls.cost_of(source, body) == expected


class TestRunnerCosts:
    def test_cost_present_charges_task_splits_proposals_and_tags(
        self, catalog, make_worker, django_capture_on_commit_callbacks
    ):
        task, sent = _run_writer(make_worker, django_capture_on_commit_callbacks, "0.000022")
        assert (task.cost_usd, task.cost_breakdown, task.cost_complete) == (
            Decimal("0.000022"),
            {"completion": "0.00002200"},
            True,
        )
        proposals = ContentProposal.objects.filter(task=task)
        assert [p.cost_usd for p in proposals] == [Decimal("0.000011")] * 2
        assert [p.revisions.get().cost_usd for p in proposals] == [Decimal("0.000011")] * 2
        assert sent[0]["tags"] == ["enrichment.write", f"task:{task.pk}"]

    def test_missing_cost_stays_none_and_marks_incomplete(
        self, catalog, make_worker, django_capture_on_commit_callbacks
    ):
        task, _ = _run_writer(make_worker, django_capture_on_commit_callbacks, None)
        assert (task.cost_usd, task.cost_breakdown, task.cost_complete) == (None, {}, False)
        assert {p.cost_usd for p in ContentProposal.objects.filter(task=task)} == {None}

    def test_template_goes_into_the_selector_tag(self):
        task = SimpleNamespace(pk=7, params={"template": "Meta Opisy!"})
        assert toolbox_calls._tags("write", task) == ["enrichment.write.meta-opisy-", "task:7"]
        assert toolbox_calls._tags("pick", None) == ["enrichment.pick"]


class TestLedger:
    def test_unknown_cost_never_becomes_zero(self, db):
        task = EnrichmentTask.objects.create(type="t")
        cost_service.charge(task, "completion", None)
        cost_service.charge(task, "translate", Decimal("0.25"))
        task.refresh_from_db()
        assert (task.cost_usd, task.cost_breakdown, task.cost_complete) == (
            Decimal("0.25"),
            {"translate": "0.25"},
            False,
        )

    def test_stale_instances_do_not_lose_updates(self, db):
        task = EnrichmentTask.objects.create(type="t")
        first, second = EnrichmentTask.objects.get(pk=task.pk), EnrichmentTask.objects.get(pk=task.pk)
        cost_service.charge(first, "completion", Decimal("0.1"))
        cost_service.charge(second, "completion", Decimal("0.2"))
        cost_service.charge(first, "media", Decimal("1"))
        task.refresh_from_db()
        assert task.cost_usd == Decimal("1.3")
        assert task.cost_breakdown == {"completion": "0.3", "media": "1"}
        assert first.cost_usd == Decimal("1.3")

    def test_no_task_or_unsaved_stand_in_is_a_no_op(self):
        cost_service.charge(None, "completion", Decimal("1"))
        cost_service.charge(SimpleNamespace(params={}), "completion", Decimal("1"))

    def test_unknown_source_is_refused(self):
        with pytest.raises(ValueError, match="source"):
            cost_service.charge(None, "billing", Decimal("1"))


class TestSplit:
    def test_even_split_and_unknown_stays_unknown(self):
        drafts = [ProposalDraft("attribute_value", {}, {}) for _ in range(3)]
        assert {d.cost_usd for d in with_cost(drafts, Decimal("0.0003"))} == {Decimal("0.0001")}
        assert {d.cost_usd for d in with_cost(drafts, None)} == {None}


class TestTranslateCost:
    def test_total_cost_usd_charged_as_translate(self, fake_adapter):
        locator = {"channel": "default", "language": "pl", "feature_idx": "name"}
        fake_adapter.set_current("SKU-1", "attribute_value", locator, {"text": "Krzesło"})
        task = EnrichmentTask.objects.create(type="t", params={})
        items = [{"key": "name", "source_text": "Krzesło", "translated_text": "Chair", "char_count": 7}]
        worker = SimpleNamespace(target_module="pim", system_prompt="", options={"fields": ["name"]})
        task.params = {"channel": "default", "source_language": "pl", "target_languages": ["en"]}
        with mock_toolbox() as router:
            router.post(TRANSLATE_URL).mock(
                return_value=httpx.Response(200, json={"items": items, "total_cost_usd": "0.004"})
            )
            drafts = TranslateKind().run(worker, task, "SKU-1")
        task.refresh_from_db()
        assert drafts[0].cost_usd == Decimal("0.004")
        assert (task.cost_usd, task.cost_breakdown) == (Decimal("0.004"), {"translate": "0.00400000"})


class TestFailedCallCost:
    def test_completion_error_marks_task_incomplete(self):
        task = EnrichmentTask.objects.create(type="t")
        with mock_toolbox() as router, pytest.raises(ToolboxError):
            router["complete"].mock(return_value=httpx.Response(400, json={"error": "BAD", "detail": "no"}))
            toolbox_calls.complete(system="", user="u", json_schema={}, model="m", kind="write", task=task)
        task.refresh_from_db()
        assert (task.cost_usd, task.cost_complete) == (None, False)


class TestMediaCost:
    URL = "http://toolbox.test/api/ai-media/v2/admin/shop/videos/"

    def test_failed_job_charges_its_reported_cost(self, monkeypatch):
        task = self._video_task(monkeypatch, {"status": "failed", "cost": "0.1", "error": {"error": "PROVIDER"}})
        assert (task.cost_breakdown, task.cost_complete) == ({"media": "0.10000000"}, True)

    def test_failed_job_without_cost_marks_task_incomplete(self, monkeypatch):
        task = self._video_task(monkeypatch, {"status": "failed"})
        assert (task.cost_usd, task.cost_complete) == (None, False)

    def test_timeout_after_submit_marks_task_incomplete(self, monkeypatch, settings):
        settings.ENRICHMENT_VIDEO_TIMEOUT_S = 0
        task = self._video_task(monkeypatch, {"status": "running"}, raises=TimeoutError)
        assert task.cost_complete is False

    def _video_task(self, monkeypatch, job: dict, raises: type[Exception] = ValueError) -> EnrichmentTask:
        monkeypatch.setattr(toolbox_calls.time, "sleep", lambda _s: None)
        task = EnrichmentTask.objects.create(type="t")
        with respx.mock(assert_all_called=False) as router, pytest.raises(raises):
            router.post(self.URL).mock(return_value=httpx.Response(202, json={"job_id": "j1"}))
            router.get(f"{self.URL}j1/").mock(return_value=httpx.Response(200, json=job))
            toolbox_calls.generate_video(image=b"png", mime="image/png", form={}, task=task)
        task.refresh_from_db()
        return task

    def test_done_job_cost_is_charged_as_media(self, monkeypatch):
        monkeypatch.setattr(toolbox_calls.time, "sleep", lambda _s: None)
        task = EnrichmentTask.objects.create(type="t")
        with respx.mock() as router:
            router.post(self.URL).mock(return_value=httpx.Response(202, json={"job_id": "j1"}))
            router.get(f"{self.URL}j1/").mock(return_value=httpx.Response(200, json={"status": "done", "cost": "0.4"}))
            router.get(f"{self.URL}j1/download/").mock(return_value=httpx.Response(200, content=b"mp4"))
            video = toolbox_calls.generate_video(image=b"png", mime="image/png", form={}, task=task)
        task.refresh_from_db()
        assert (video.value, video.cost) == (b"mp4", Decimal("0.4"))
        assert task.cost_breakdown == {"media": "0.40000000"}


class TestApi:
    def test_task_proposal_and_revision_expose_costs(self, admin_client, fake_adapter):
        task = EnrichmentTask.objects.create(type="t")
        proposal = proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator={"channel": "default", "language": "pl", "feature_idx": "description"},
            proposed_value={"text": "Copy."},
            task=task,
            cost_usd=Decimal("0.001"),
        )
        with mock_toolbox() as router:
            router["complete"].mock(
                return_value=httpx.Response(200, json={**CANNED_COMPLETION, "parsed": {"text": "Short."}})
            )
            revised = admin_client.post(
                f"{BASE}/proposals/{proposal.pk}/revise/", {"quick_action": "shorten"}, format="json"
            )
        assert revised.json()["cost_usd"] == "0.00002200"
        assert admin_client.get(f"{BASE}/proposals/{proposal.pk}/").json()["cost_usd"] == "0.00100000"
        body = admin_client.get(f"{BASE}/tasks/{task.pk}/").json()
        assert (body["cost_usd"], body["cost_breakdown"], body["cost_complete"]) == (
            "0.00002200",
            {"completion": "0.00002200"},
            True,
        )
        assert admin_client.get(f"{BASE}/proposals/{proposal.pk}/revisions/").json()[0]["cost_usd"] == "0.00100000"
