# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""`WorkflowDefinition.clean()` and `ContentProposal.result_ref` (adapter `apply` returning `{"subject_ref"}`)."""

import pytest
from django.core.exceptions import ValidationError

from django_enrichment.models import WorkflowDefinition
from django_enrichment.services import apply_service, proposal_service
from tests import workflow_setup as wf

pytestmark = pytest.mark.django_db


@pytest.fixture
def templates(fake_adapter):
    wf.make_templates()


def _errors(steps: list, inputs_schema: object = None, target_module: str = wf.DOC_MODULE) -> dict:
    definition = WorkflowDefinition(
        key="d", label="D", target_module=target_module, steps=steps, inputs_schema=inputs_schema or {}
    )
    with pytest.raises(ValidationError) as exc:
        definition.full_clean()
    return exc.value.message_dict


@pytest.mark.usefixtures("templates")
class TestDefinitionValidation:
    def test_a_valid_definition_passes(self):
        steps = [
            wf.step("ask", output="questions"),
            wf.step("draft", needs=("ask",)),
            wf.step("compose", needs=("draft",), output="document"),
        ]
        wf.make_definition("ok", steps, {"type": "object", "properties": {"topic": {"type": "string"}}})

    @pytest.mark.parametrize(
        ("steps", "message"),
        [
            ([], "at least one step"),
            ([wf.step("draft", needs=("outline",)), wf.step("outline")], "not an earlier step"),
            ([wf.step("draft"), wf.step("draft")], "duplicate key"),
            ([wf.step("ask", output="questions", gate="auto")], "needs the review gate"),
            ([wf.step("x", template="missing")], "unknown template"),
            ([wf.step("ask", output="artifact")], "needs a 'generate' worker"),
            ([wf.step("Bad Key", template="draft")], "key"),
            ([{"key": "a", "label": "A", "template": "draft", "extra": 1}], "extra"),
            ("not a list", "list"),
        ],
    )
    def test_steps_errors(self, steps, message):
        assert message in " ".join(_errors(steps)["steps"])

    def test_document_goes_to_the_definition_target_module(self):
        assert "must go to 'pim'" in " ".join(
            _errors([wf.step("compose", output="document")], target_module="pim")["steps"]
        )

    @pytest.mark.parametrize(
        ("schema", "message"),
        [
            ({"type": "array"}, '"type": "object"'),
            ({"type": "nope"}, "invalid JSON Schema"),
            ([1], "JSON Schema object"),
        ],
    )
    def test_inputs_schema_errors(self, schema, message):
        assert message in " ".join(_errors([wf.step("draft")], schema)["inputs_schema"])


class TestResultRef:
    def test_apply_returning_a_subject_ref_is_kept(self, fake_adapter):
        value = {"language": "en", "title": "T", "slug": "t", "body_markdown": "B", "meta": {}, "faq": []}
        proposal = proposal_service.intake(
            target_module="pim", subject_ref="run:x", target_kind="document", target_locator={}, proposed_value=value
        )
        applied = apply_service.apply(proposal)
        applied.refresh_from_db()
        assert (applied.status, applied.result_ref) == ("applied", "doc-1")
        assert fake_adapter.documents()["doc-1"] == value

    def test_plain_apply_leaves_it_blank(self, fake_adapter):
        proposal = proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator={},
            proposed_value={"text": "x"},
        )
        assert apply_service.apply(proposal).result_ref == ""
