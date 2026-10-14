# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Revisions, AI corrections, rules and batch correction — services against the fake adapter and a mocked toolbox."""

import json

import httpx
import pytest
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import EnrichmentTask, ProposalRevision
from django_enrichment.services import (
    proposal_service,
    revise_batch_service,
    revise_service,
    revision_service,
    rules_service,
)
from django_enrichment.services.revise_values import RevisionNotApplicableError

LOCATOR = {"channel": "default", "language": "pl", "feature_idx": "description"}
LONG = "Solid steel body. " * 20 + "The warranty lasts one year."
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]


def _answer(parsed: dict) -> httpx.Response:
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": parsed})


def _proposal(value: dict, **over):
    fields = {
        "target_module": "pim",
        "subject_ref": "SKU-1",
        "target_kind": "attribute_value",
        "target_locator": LOCATOR,
        "proposed_value": value,
    }
    return proposal_service.intake(**{**fields, **over})


def _sent(route, index: int = 0) -> dict:
    return json.loads(route.calls[index].request.content)


def _revise(proposal, parsed: dict, **kwargs):
    with mock_toolbox() as router:
        route = router["complete"].mock(return_value=_answer(parsed))
        result = revise_service.revise(proposal, **kwargs)
    return result, route


@pytest.fixture
def catalog(fake_adapter, monkeypatch):
    """Give the fake adapter a field catalog (`describe_fields`)."""
    fields = [
        {"key": "description", "label": "Description", "type": "html", "max_length": None},
        {"key": "meta_title", "label": "Meta title", "type": "text", "max_length": 60},
        {"key": "color", "label": "Color", "type": "select", "options": [{"value": "red"}, {"value": "blue"}]},
    ]
    monkeypatch.setattr(fake_adapter, "describe_fields", lambda **_: fields, raising=False)
    return fields


class TestRevisionInvariant:
    def test_intake_creates_revision_one(self, fake_adapter):
        proposal = _proposal({"text": "Copy."})
        [revision] = proposal.revisions.all()
        assert (revision.number, revision.author_kind, revision.value) == (1, "ai", {"text": "Copy."})

    def test_manual_edit_adds_user_revision_and_keeps_invariant(self, fake_adapter, admin_user):
        proposal = _proposal({"text": "Copy."})
        proposal_service.update_value(proposal, {"text": "Edited."}, user=admin_user)
        proposal.refresh_from_db()
        latest = proposal.revisions.last()
        assert (latest.number, latest.author_kind, latest.author) == (2, "user", admin_user)
        assert proposal.proposed_value == latest.value == {"text": "Edited."}

    def test_drifted_is_revisable(self, fake_adapter):
        proposal = _proposal({"text": "Copy."})
        proposal.status = ProposalStatus.DRIFTED.value
        proposal_service.update_value(proposal, {"text": "Again."})
        assert proposal.revisions.count() == 2

    @pytest.mark.parametrize("status", [ProposalStatus.APPLIED.value, ProposalStatus.REJECTED.value])
    def test_closed_proposals_are_not_revisable(self, fake_adapter, status):
        proposal = _proposal({"text": "Copy."})
        proposal.status = status
        with pytest.raises(ValueError, match="pending or drifted"):
            proposal_service.update_value(proposal, {"text": "x"})
        with pytest.raises(ValueError, match="pending or drifted"):
            revise_service.revise(proposal, quick_action="shorten")

    def test_media_is_not_correctable(self, fake_adapter):
        proposal = _proposal({"op": "replace_main"}, target_kind="picture")
        with pytest.raises(ValueError, match="media"):
            revise_service.revise(proposal, quick_action="shorten")

    def test_restore_copies_an_older_revision(self, fake_adapter, admin_user):
        proposal = _proposal({"text": "One."})
        proposal_service.update_value(proposal, {"text": "Two."})
        revision = revision_service.restore(proposal, 1, user=admin_user)
        proposal.refresh_from_db()
        assert (revision.number, revision.author_kind, revision.value) == (3, "restore", {"text": "One."})
        assert proposal.proposed_value == {"text": "One."}

    def test_restore_unknown_number_is_not_found(self, fake_adapter):
        with pytest.raises(ValueError, match="not found"):
            revision_service.restore(_proposal({"text": "One."}), 9)


class TestEdits:
    def test_edits_applied_as_ai_revision(self, fake_adapter, admin_user):
        proposal = _proposal({"text": LONG})
        edits = {"edits": [{"old": "one year", "new": "two years"}], "rewrite": None}
        revision, route = _revise(proposal, edits, instruction="Two years warranty.", user=admin_user)
        proposal.refresh_from_db()
        assert revision.value["text"].endswith("The warranty lasts two years.")
        assert proposal.proposed_value == revision.value
        assert (revision.number, revision.author_kind, revision.author) == (2, "ai", admin_user)
        assert revision.instruction == "Two years warranty."
        assert set(_sent(route)["json_schema"]["properties"]) == {"edits", "rewrite"}

    @pytest.mark.parametrize("old", ["not in the text", "Solid steel body."])
    def test_missing_or_ambiguous_edit_saves_nothing(self, fake_adapter, old):
        proposal = _proposal({"text": LONG})
        good = {"old": "one year", "new": "two years"}
        with pytest.raises(RevisionNotApplicableError):
            _revise(proposal, {"edits": [good, {"old": old, "new": "x"}], "rewrite": None}, instruction="Fix.")
        proposal.refresh_from_db()
        assert proposal.proposed_value == {"text": LONG}
        assert proposal.revisions.count() == 1

    def test_rewrite_replaces_the_value(self, fake_adapter):
        revision, _ = _revise(_proposal({"text": LONG}), {"edits": [], "rewrite": "Brand new."}, quick_action="shorten")
        assert revision.value == {"text": "Brand new."}

    def test_html_is_sanitised(self, fake_adapter):
        html = "<p>Short <strong>html</strong>.</p>"
        rewrite = '<p>Hi <a href="http://x">link</a></p><script>alert(1)</script>'
        revision, route = _revise(_proposal({"text": html}), {"edits": [], "rewrite": rewrite}, instruction="Go.")
        assert revision.value == {"text": "<p>Hi link</p>"}
        assert "edits" in _sent(route)["json_schema"]["properties"]


class TestShapesAndScope:
    def test_short_text_uses_full_value_schema_with_catalog_limit(self, catalog):
        proposal = _proposal({"text": "Old title"}, target_locator={**LOCATOR, "feature_idx": "meta_title"})
        revision, route = _revise(proposal, {"text": "New title"}, quick_action="formal")
        assert revision.value == {"text": "New title"}
        schema = _sent(route)["json_schema"]
        assert schema["properties"]["text"] == {"type": "string", "maxLength": 60}
        assert '"label": "Meta title"' in _sent(route)["messages"][-1]["content"]

    def test_option_enum_from_catalog(self, catalog):
        proposal = _proposal({"option": "red"}, target_locator={**LOCATOR, "feature_idx": "color"})
        revision, route = _revise(proposal, {"option": "blue"}, instruction="Blue.")
        assert revision.value == {"option": "blue"}
        assert _sent(route)["json_schema"]["properties"]["option"]["enum"] == ["red", "blue"]

    def test_option_outside_catalog_is_not_applicable(self, catalog):
        proposal = _proposal({"option": "red"}, target_locator={**LOCATOR, "feature_idx": "color"})
        with pytest.raises(RevisionNotApplicableError):
            _revise(proposal, {"option": "green"}, instruction="Green.")

    def test_selection_scope_is_sent_and_stored(self, fake_adapter):
        proposal = _proposal({"text": LONG})
        edits = {"edits": [{"old": "one year", "new": "two years"}], "rewrite": None}
        scope = {"type": "selection", "text": "The warranty lasts one year."}
        revision, route = _revise(proposal, edits, instruction="Longer.", scope=scope)
        assert revision.scope == scope
        assert "<selection>\nThe warranty lasts one year.\n</selection>" in _sent(route)["messages"][-1]["content"]

    @pytest.mark.parametrize("text", ["absent fragment", "Solid steel body.", ""])
    def test_selection_must_occur_once(self, fake_adapter, text):
        with pytest.raises(ValueError, match="exactly once"):
            revise_service.revise(_proposal({"text": LONG}), instruction="x", scope={"type": "selection", "text": text})

    def test_instruction_or_quick_action_required(self, fake_adapter):
        with pytest.raises(ValueError, match="instruction"):
            revise_service.revise(_proposal({"text": "Copy."}), instruction="  ")

    def test_variants_return_candidates_and_save_nothing(self, fake_adapter):
        proposal = _proposal({"text": "Copy."})
        result, route = _revise(proposal, {"candidates": ["A", "B", "C", "D"]}, quick_action="variants")
        assert result == [{"text": "A"}, {"text": "B"}, {"text": "C"}]
        assert proposal.revisions.count() == 1
        assert _sent(route)["json_schema"]["properties"]["candidates"]["maxItems"] == 3


class TestPrompt:
    def test_prompt_layers_and_worker_model(self, fake_adapter, make_worker):
        worker = make_worker(
            key="pim.writer",
            kind="field",
            system_prompt="You write product copy.",
            model="fake-chat",
            options={"field": "description", "rules": ["No emojis."]},
        )
        task = EnrichmentTask.objects.create(type="pim.writer", params={"prompt": "Highlight durability."})
        proposal = _proposal({"text": "Copy."}, task=task, source=f"worker:pim.writer@v{worker.version}")
        rules_service.set_rules(proposal, ["Keep it short."])
        _, route = _revise(proposal, {"text": "Better."}, quick_action="shorten", instruction="Mention steel.")
        body = _sent(route)
        user = body["messages"][-1]["content"]
        assert body["model"] == "fake-chat"
        assert body["messages"][0] == {"role": "system", "content": "You write product copy."}
        order = ["- Keep it short.\n- No emojis.", "Highlight durability.", "Copy.", "shorter", "Mention steel."]
        positions = [user.index(part) for part in order]
        assert positions == sorted(positions)


class TestRules:
    def test_limits(self, fake_adapter):
        proposal = _proposal({"text": "Copy."})
        with pytest.raises(ValueError, match="at most 20"):
            rules_service.set_rules(proposal, [f"r{i}" for i in range(21)])
        with pytest.raises(ValueError, match="200 characters"):
            rules_service.set_rules(proposal, ["x" * 201])
        assert rules_service.set_rules(proposal, [" a ", ""]).rules == ["a"]

    def test_promote_to_worker_bumps_version(self, fake_adapter, make_worker):
        worker = make_worker(key="pim.writer", kind="field", options={"field": "description"})
        proposal = _proposal({"text": "Copy."}, source="worker:pim.writer@v1")
        assert rules_service.promote(proposal, "No emojis.", target="worker") == ["No emojis."]
        worker.refresh_from_db()
        assert (worker.options["rules"], worker.version) == (["No emojis."], 2)

    def test_promote_to_template_without_template_fails(self, fake_adapter):
        with pytest.raises(ValueError, match="no template"):
            rules_service.promote(_proposal({"text": "Copy."}), "No emojis.", target="template")


class TestBatch:
    def test_one_failing_proposal_is_recorded(self, fake_adapter, admin_user):
        task = EnrichmentTask.objects.create(type="pim.writer")
        good = [_proposal({"text": f"Copy {i}."}, task=task, subject_ref=f"SKU-{i}") for i in (1, 2)]
        _proposal({"op": "odd"}, task=task, subject_ref="SKU-3")
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_answer({"text": "Fixed."}))
            queued = revise_batch_service.queue(task, {"instruction": "Fix."}, user=admin_user)
        task.refresh_from_db()
        assert queued == 3
        assert (task.counts["revised"], task.counts["failed"]) == (2, 1)
        assert task.errors[0]["subject_ref"] == "SKU-3"
        assert task.errors[0]["code"] == "INTERNAL_ERROR"
        assert ProposalRevision.objects.filter(proposal__in=good, author_kind="ai", number=2).count() == 2

    def test_variants_cannot_be_batched(self, fake_adapter):
        task = EnrichmentTask.objects.create(type="pim.writer")
        with pytest.raises(ValueError, match="variants"):
            revise_batch_service.queue(task, {"quick_action": "variants"})
