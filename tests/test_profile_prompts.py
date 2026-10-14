# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The company profile and memory in real prompts: kinds write / pick / generate / questions and corrections.

Profile instructions go to the system message, the facts table and memory to fenced data blocks; tasks and
revisions record the profile versions they used.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.kinds.generate import GenerateKind
from django_enrichment.kinds.pick import PickKind
from django_enrichment.kinds.questions import QuestionsKind
from django_enrichment.kinds.write import WriteKind
from django_enrichment.models import EnrichmentTask, MemoryEntry
from django_enrichment.services import memory_service, profile_service, proposal_service, revise_service
from tests import workflow_setup as wf

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]
PROFILE = {
    "brand": {"differentiators": ["Made to order"]},
    "language": {"spellings": [{"wrong": "e-commerce", "right": "ecommerce"}]},
    "facts": [{"id": "f1", "label": "Founded", "value": "1999"}],
    "always_rules": ["Never promise delivery dates."],
}
CATALOG = [
    {"key": "meta_title", "label": "Meta title", "type": "text", "max_length": 60},
    {"key": "color", "label": "Color", "type": "select", "options": [{"value": "red"}, {"value": "blue"}]},
]


@pytest.fixture
def profile():
    profile_service.save_profile(channel="", data=PROFILE)
    return profile_service.version_ids("default")


def _worker(**over) -> SimpleNamespace:
    fields = {"key": "pim.x", "version": 1, "target_module": "pim", "system_prompt": "Sys.", "model": "m"}
    return SimpleNamespace(**{**fields, "options": {}, **over})


def _remember(kind: str, title: str, **over) -> None:
    memory_service.remember(MemoryEntry(kind=kind, title=title, text=f"{title} text", **over))


def _call(kind, worker, task, subject_ref: str, parsed: dict | None = None) -> tuple[str, str]:
    with mock_toolbox() as router:
        if parsed is None:
            route = router["complete"].mock(side_effect=wf.respond)
        else:
            route = router["complete"].mock(
                return_value=httpx.Response(200, json={**CANNED_COMPLETION, "parsed": parsed})
            )
        kind().run(worker, task, subject_ref)
    system, user = (message["content"] for message in json.loads(route.calls[0].request.content)["messages"])
    return system, user


class TestContentKinds:
    def test_write_sees_profile_and_subject_memory(self, fake_adapter, profile):
        fake_adapter.set_catalog(CATALOG)
        _remember("seo_brief", "Brief", subject_module="pim", subject_ref="SKU-1")
        task = EnrichmentTask.objects.create(
            type="pim.writer", params={"channel": "default", "fields": ["meta_title"], "languages": ["pl"]}
        )
        system, user = _call(WriteKind, _worker(), task, "SKU-1", parsed={"meta_title": "T"})
        assert system.startswith("Sys.\n\n") and "Never promise delivery dates." in system and "## Brand" in system
        assert "<company_profile>\n| id | label | value" in user and "| f1 | Founded | 1999 |" in user
        assert "<memory>\n- [seo_brief] Brief: Brief text\n</memory>" in user
        task.refresh_from_db()
        assert task.profile_versions == profile

    def test_pick_routes_brand_and_language_only(self, fake_adapter, profile):
        fake_adapter.set_catalog(CATALOG)
        task = EnrichmentTask.objects.create(type="pim.tagger", params={"channel": "default", "fields": ["color"]})
        system, user = _call(PickKind, _worker(), task, "SKU-1", parsed={"color": "red"})
        assert "write 'ecommerce', not 'e-commerce'" in system
        assert "company_profile" not in user  # facts are not routed to pick

    def test_no_profile_no_stamp(self, fake_adapter):
        fake_adapter.set_catalog(CATALOG)
        task = EnrichmentTask.objects.create(
            type="pim.writer", params={"channel": "default", "fields": ["meta_title"], "languages": ["pl"]}
        )
        system, user = _call(WriteKind, _worker(), task, "SKU-1", parsed={"meta_title": "T"})
        assert system == "Sys." and "company_profile" not in user and "<memory>" not in user
        task.refresh_from_db()
        assert task.profile_versions == []


class TestStepKinds:
    def test_generate_sees_profile_and_run_memory(self, profile):
        _remember("seo_brief", "Run brief", subject_module="enrichment", subject_ref="run:abc")
        task = EnrichmentTask.objects.create(type="wf.write", params={"run": "abc", "channel": "default"})
        system, user = _call(GenerateKind, _worker(), task, "run:abc")
        assert "Never promise delivery dates." in system
        assert "| f1 | Founded | 1999 |" in user and "<memory>\n- [seo_brief] Run brief" in user

    def test_questions_see_facts_and_remembered_answers(self, profile):
        _remember("answer", "Since when?", channel="default")
        task = EnrichmentTask.objects.create(type="wf.ask", params={"channel": "default"})
        _, user = _call(QuestionsKind, _worker(), task, "run:abc")
        assert "| f1 | Founded | 1999 |" in user
        assert "<remembered_answers>\n- [answer] Since when?: Since when? text\n</remembered_answers>" in user


class TestCorrections:
    def test_correction_sees_profile_and_records_versions(self, fake_adapter, make_worker, profile):
        make_worker(key="pim.writer", kind="write", system_prompt="Sys.", options={})
        _remember("seo_brief", "Brief", subject_module="pim", subject_ref="SKU-1")
        task = EnrichmentTask.objects.create(type="pim.writer", params={"channel": "default"}, profile_versions=[1])
        proposal = proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator={"channel": "default", "language": "pl", "feature_idx": "description"},
            proposed_value={"text": "Copy."},
            task=task,
            source="worker:pim.writer@v1",
        )
        assert proposal.revisions.get(number=1).profile_versions == [1]
        with mock_toolbox() as router:
            route = router["complete"].mock(
                return_value=httpx.Response(200, json={**CANNED_COMPLETION, "parsed": {"text": "Better."}})
            )
            revision = revise_service.revise(proposal, instruction="Fix.")
        system, user = (m["content"] for m in json.loads(route.calls[0].request.content)["messages"])
        assert "Never promise delivery dates." in system and "## Brand" in system
        assert "| f1 | Founded | 1999 |" in user and "- [seo_brief] Brief" in user
        assert revision.profile_versions == profile
