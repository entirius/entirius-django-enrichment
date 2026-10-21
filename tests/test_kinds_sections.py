# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `sections`: an article body written section by section from the approved outline, then its lead and
closing — every call sees the sections written before it."""

import json
import logging
import re
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.kinds.sections import SectionsKind, outline_sections, with_heading
from django_enrichment.models import EnrichmentTask

OUTLINE = (
    "# Linen sofa care\n**Thesis:** Care is cheap.\n\n"
    "### 1. Brushing\n- angle: weekly\n- material: (answer: since)\n\n"
    "### 2. Stains\n- angle: act fast\n"
)
BODIES = {1: "## Brushing\n\nBrush it weekly.", 2: "Blot a stain at once."}
PREVIOUS = (
    "Old lead.\n\n## Brushing\n\nOld brushing.\n\n## Stains\n\nOld stains.\n\n## Closing thoughts\n\nOld closing."
)
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]


def _answer(request: httpx.Request, cost: str | None = "0.01") -> httpx.Response:
    body = json.loads(request.content)
    if "lead" in body["json_schema"]["properties"]:
        parsed = {"lead": "Linen forgives a lot.", "closing": "What will you brush first?"}
    elif match := re.search(r"Write section (\d+) of", body["messages"][-1]["content"]):
        parsed = {"text": BODIES[int(match.group(1))]}
    else:
        parsed = {"text": "One body, one call."}
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": parsed, "cost": cost})


def _run(params: dict, answer=_answer) -> tuple[list, list[dict]]:
    worker = SimpleNamespace(system_prompt="Write articles.", model="fake-chat", options={})
    task = EnrichmentTask.objects.create(type="t", params={"step": "draft", **params})
    with mock_toolbox() as router:
        route = router["complete"].mock(side_effect=answer)
        drafts = SectionsKind().run(worker, task, "run:abc")
    return drafts, [json.loads(call.request.content) for call in route.calls]


def _user(call: dict) -> str:
    return call["messages"][-1]["content"]


INPUTS = {"inputs": {"outline": {"text": OUTLINE}}}


class TestOutline:
    def test_sections_are_the_numbered_headings_with_their_lines(self):
        sections = outline_sections(OUTLINE)
        assert [(s.number, s.title) for s in sections] == [(1, "Brushing"), (2, "Stains")]
        assert sections[0].block == "### 1. Brushing\n- angle: weekly\n- material: (answer: since)"

    def test_an_outline_without_numbered_headings_has_no_sections(self):
        assert outline_sections("# Title\n\n## Frame\n\n## Care") == []


class TestSections:
    def test_one_call_per_section_then_the_lead_and_closing(self):
        drafts, calls = _run(INPUTS)
        assert len(calls) == 3
        assert {call["tags"][0] for call in calls} == {"enrichment.sections"}
        assert drafts[0].target_locator == {"step": "draft"}

    def test_each_section_call_sees_its_outline_block_and_the_sections_before(self):
        _, calls = _run(INPUTS)
        first, second, _ = map(_user, calls)
        assert "Write section 1 of 2" in first and "### 1. Brushing\n- angle: weekly" in first
        assert "<sections_so_far>" not in first
        assert "Write section 2 of 2" in second and "### 2. Stains" in second
        assert "<sections_so_far>\n## Brushing\n\nBrush it weekly.\n</sections_so_far>" in second

    def test_the_lead_and_closing_are_written_from_the_finished_sections(self):
        _, calls = _run(INPUTS)
        assert "## Brushing\n\nBrush it weekly.\n\n## Stains\n\nBlot a stain at once." in _user(calls[-1])

    def test_the_body_is_lead_sections_closing_with_every_heading(self):
        drafts, _ = _run(INPUTS)
        assert drafts[0].proposed_value == {
            "text": "Linen forgives a lot.\n\n## Brushing\n\nBrush it weekly.\n\n## Stains\n\nBlot a stain at once."
            "\n\nWhat will you brush first?"
        }

    def test_the_cost_is_the_sum_of_the_calls(self):
        drafts, _ = _run(INPUTS)
        assert drafts[0].cost_usd == Decimal("0.03")

    def test_one_unknown_cost_makes_the_cost_unknown(self):
        drafts, _ = _run(INPUTS, answer=lambda request: _answer(request, cost=None))
        assert drafts[0].cost_usd is None

    def test_an_operator_instruction_reaches_every_call(self):
        _, calls = _run({**INPUTS, "instruction": "Shorter.", "previous": {"text": "Old body."}})
        assert all("Operator instruction: Shorter." in _user(call) for call in calls)

    def test_a_regenerated_section_ends_on_its_own_task_and_sees_its_previous_section(self):
        _, calls = _run({**INPUTS, "instruction": "Shorter.", "previous": {"text": PREVIOUS}})
        first, second, ends = map(_user, calls)
        assert first.rstrip().endswith("Answer with the section as markdown in the `text` property.")
        assert "<previous_version>\n## Brushing\n\nOld brushing.\n</previous_version>" in first
        assert "Old stains." not in first and "Old lead." not in first
        assert "<previous_version>\n## Stains\n\nOld stains.\n</previous_version>" in second
        assert f"<previous_version>\n{PREVIOUS}\n</previous_version>" in ends
        assert "Write a new version" not in first + second + ends

    def test_a_section_missing_from_the_previous_version_gets_none(self):
        _, calls = _run({**INPUTS, "instruction": "More detail.", "previous": {"text": "## Other\n\nOld."}})
        assert "<previous_version>" not in _user(calls[0])

    def test_brief_language_reaches_every_call(self):
        _, calls = _run({**INPUTS, "brief": {"topic": "Sofa", "language": "pl"}})
        assert all("ISO 639-1 code `pl`" in _user(call) for call in calls)

    def test_an_outline_without_sections_is_written_in_one_call(self, caplog):
        with caplog.at_level(logging.WARNING, logger="process"):
            drafts, calls = _run({"inputs": {"outline": {"text": "# Title\n\n## Frame"}}})
        assert len(calls) == 1
        assert drafts[0].proposed_value == {"text": "One body, one call."}
        assert "the lead, every section of the outline and the closing" in _user(calls[0])
        assert "no numbered" in caplog.text

    def test_the_closing_may_carry_its_own_heading(self):
        _, calls = _run(INPUTS)
        assert "No headings" not in _user(calls[-1])


class TestSectionHeading:
    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ("## Brushing\n\nBrush it weekly.", "## Brushing\n\nBrush it weekly."),
            ("Brush it weekly.", "## Brushing\n\nBrush it weekly."),
            ("# Linen sofa care\n\n## 1. Brushing\n\nBrush it weekly.", "## Brushing\n\nBrush it weekly."),
            ("## 1. Brushing weekly\n\nBrush it.", "## Brushing\n\nBrush it."),
            ("### Tools\n\nA soft brush.", "## Brushing\n\n### Tools\n\nA soft brush."),
        ],
    )
    def test_every_section_starts_with_its_outline_title(self, answer, expected):
        assert with_heading(answer, "Brushing") == expected
