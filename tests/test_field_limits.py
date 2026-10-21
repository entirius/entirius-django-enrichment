# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Character ranges of short fields (`options.field_limits` of kind `write`): prompt, one corrective call, QA `LIMIT`."""

import json
import logging
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from django.core.exceptions import ValidationError
from django_utils.toolbox.testing import CANNED_COMPLETION, error_response, mock_toolbox

from django_enrichment.kinds.field_limits import FieldLimit, configured
from django_enrichment.kinds.write import WriteKind
from django_enrichment.models import ContentProposal
from django_enrichment.services import proposal_service, qa_service, task_service, toolbox_calls
from django_enrichment.services.queue_types import LeaseLost

CATALOG = [
    {"key": "meta_title", "label": "Meta title", "type": "text", "max_length": 255, "translatable": True},
    {"key": "meta_description", "label": "Meta description", "type": "text", "max_length": 255, "translatable": True},
    {"key": "description", "label": "Long description", "type": "html", "max_length": None, "translatable": True},
]
LIMITS = {"meta_title": {"min": 40, "max": 60}, "meta_description": {"min": 120, "max": 160}}
PARAMS = {"channel": "default", "fields": ["meta_title", "meta_description"], "languages": ["pl"]}
GOOD_TITLE = "t" * 50
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]


def _text(length: int) -> str:
    return "w" * length


def _answer(parsed: dict, cost: str | None = "0.000022") -> httpx.Response:
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": parsed, "cost": cost})


def _sent(route, index: int) -> dict:
    return json.loads(route.calls[index].request.content)


def _user(route, index: int) -> str:
    return _sent(route, index)["messages"][-1]["content"]


@pytest.fixture
def catalog(fake_adapter):
    fake_adapter.set_catalog(CATALOG)
    return fake_adapter


def _run_kind(answers: list, *, options: dict | None = None, params: dict | None = None):
    """`WriteKind.run` against a mocked toolbox that answers the completions one after another."""
    worker = SimpleNamespace(
        key="pim.writer",
        version=1,
        target_module="pim",
        system_prompt="Be precise.",
        model="m",
        options={"rules": ["No prices."], "field_limits": LIMITS} if options is None else options,
    )
    task = SimpleNamespace(type="pim.writer", params={**PARAMS, **(params or {})})
    with mock_toolbox() as router:
        route = router["complete"].mock(side_effect=answers)
        drafts = WriteKind().run(worker, task, "SKU-1")
    return drafts, route


def _texts(drafts) -> dict[str, str]:
    return {draft.target_locator["feature_idx"]: draft.proposed_value["text"] for draft in drafts}


class TestFieldLimitOption:
    def test_range_defaults_to_no_minimum(self):
        assert FieldLimit(max=60).min == 0

    @pytest.mark.parametrize("raw", [{"min": 200, "max": 160}, {"min": 120}, {"min": -1, "max": 10}, {"max": 0}])
    def test_invalid_ranges_are_refused(self, raw, make_worker):
        with pytest.raises(ValidationError):
            make_worker(key="pim.writer", kind="write", options={"field_limits": {"meta_description": raw}})

    def test_worker_keeps_the_ranges(self, make_worker):
        worker = make_worker(key="pim.writer", kind="write", options={"field_limits": LIMITS})
        assert configured(worker.options)["meta_description"] == FieldLimit(min=120, max=160)

    def test_no_limits_configured_is_an_empty_mapping(self):
        assert configured({}) == configured(None) == {}


class TestPrompt:
    def test_range_is_in_the_field_line_and_the_schema_hint(self, catalog):
        _, route = _run_kind([_answer({"meta_title": GOOD_TITLE, "meta_description": _text(150)})])
        user = _user(route, 0)
        properties = _sent(route, 0)["json_schema"]["properties"]
        assert '"meta_description": Meta description; text; between 120 and 160 characters, spaces included' in user
        assert '"meta_title": Meta title; text; between 40 and 60 characters, spaces included' in user
        assert "at most 255" not in user
        assert properties["meta_description"]["description"] == "(min 120 chars, max 160 chars)"

    def test_range_sits_before_the_prose_rule(self, catalog):
        options = {"field_limits": LIMITS, "field_rules": {"meta_title": "Product name first."}}
        _, route = _run_kind([_answer({"meta_title": GOOD_TITLE, "meta_description": _text(150)})], options=options)
        assert "between 40 and 60 characters, spaces included; Product name first." in _user(route, 0)

    def test_maximum_only_reads_at_most(self, catalog):
        options = {"field_limits": {"meta_title": {"max": 60}}}
        _, route = _run_kind([_answer({"meta_title": GOOD_TITLE, "meta_description": "d"})], options=options)
        assert '"meta_title": Meta title; text; at most 60 characters' in _user(route, 0)
        assert _sent(route, 0)["json_schema"]["properties"]["meta_title"]["description"] == "(max 60 chars)"

    def test_field_without_a_range_keeps_the_catalog_maximum(self, catalog):
        options = {"field_limits": {"meta_title": {"min": 40, "max": 60}}}
        _, route = _run_kind([_answer({"meta_title": GOOD_TITLE, "meta_description": "d"})], options=options)
        assert '"meta_description": Meta description; text; at most 255 characters' in _user(route, 0)


class TestCorrectiveCall:
    def test_in_range_values_cost_no_extra_call(self, catalog):
        answers = [_answer({"meta_title": GOOD_TITLE, "meta_description": _text(150)})]
        drafts, route = _run_kind(answers)
        assert route.call_count == 1
        assert _texts(drafts) == {"meta_title": GOOD_TITLE, "meta_description": _text(150)}

    def test_out_of_range_field_is_rewritten_once_and_the_new_value_used(self, catalog):
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}),
            _answer({"meta_description": _text(150)}),
        ]
        drafts, route = _run_kind(answers)
        assert route.call_count == 2
        assert _texts(drafts) == {"meta_title": GOOD_TITLE, "meta_description": _text(150)}

    def test_the_rewrite_asks_for_the_offending_fields_only_with_their_numbers(self, catalog):
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}),
            _answer({"meta_description": _text(150)}),
        ]
        _, route = _run_kind(answers)
        second = _sent(route, 1)
        user = _user(route, 1)
        assert second["json_schema"]["required"] == ["meta_description"]
        assert second["messages"][0]["content"] == "Be precise."
        assert "<current_values>" in user and _text(195) in user and GOOD_TITLE not in user
        assert "between 120 and 160 characters, spaces included; now 195 characters, 35 too long" in user
        assert "- No prices." in user
        assert second["tags"][0] == "enrichment.revise"

    def test_too_short_is_rewritten_too(self, catalog):
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(100)}),
            _answer({"meta_description": _text(140)}),
        ]
        drafts, route = _run_kind(answers)
        assert "now 100 characters, 20 too short" in _user(route, 1)
        assert _texts(drafts)["meta_description"] == _text(140)

    def test_both_fields_out_of_range_share_the_one_rewrite(self, catalog):
        answers = [
            _answer({"meta_title": _text(80), "meta_description": _text(195)}),
            _answer({"meta_title": _text(55), "meta_description": _text(150)}),
        ]
        drafts, route = _run_kind(answers)
        assert route.call_count == 2
        assert _sent(route, 1)["json_schema"]["required"] == ["meta_title", "meta_description"]
        assert _texts(drafts) == {"meta_title": _text(55), "meta_description": _text(150)}

    def test_still_out_of_range_is_accepted_without_a_third_call(self, catalog):
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}),
            _answer({"meta_description": _text(170)}),
            _answer({"meta_description": _text(150)}),
        ]
        drafts, route = _run_kind(answers)
        assert route.call_count == 2
        assert _texts(drafts)["meta_description"] == _text(170)

    def test_long_fields_are_never_rewritten(self, catalog):
        options = {"field_limits": {"description": {"min": 500, "max": 2000}}}
        params = {"fields": ["description"]}
        drafts, route = _run_kind([_answer({"description": "<p>Short.</p>"})], options=options, params=params)
        assert route.call_count == 1
        assert _texts(drafts) == {"description": "<p>Short.</p>"}

    def test_a_field_without_a_range_is_never_rewritten(self, catalog):
        drafts, route = _run_kind([_answer({"meta_title": _text(200), "meta_description": "d"})], options={})
        assert route.call_count == 1
        assert _texts(drafts)["meta_title"] == _text(200)

    def test_a_field_at_the_boundary_is_in_range(self, catalog):
        answers = [_answer({"meta_title": _text(40), "meta_description": _text(160)})]
        _, route = _run_kind(answers)
        assert route.call_count == 1

    def test_a_blank_or_missing_rewrite_keeps_the_original(self, catalog):
        answers = [
            _answer({"meta_title": _text(80), "meta_description": _text(195)}),
            _answer({"meta_title": "  "}),
        ]
        drafts, route = _run_kind(answers)
        assert route.call_count == 2
        assert _texts(drafts) == {"meta_title": _text(80), "meta_description": _text(195)}

    def test_each_language_gets_its_own_rewrite(self, catalog):
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}),
            _answer({"meta_description": _text(150)}),
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(150)}),
        ]
        drafts, route = _run_kind(answers, params={"languages": ["pl", "en"]})
        assert route.call_count == 3
        assert [d.target_locator["language"] for d in drafts] == ["pl", "pl", "en", "en"]


class TestCorrectiveCallFails:
    def test_failed_rewrite_keeps_the_original_and_never_fails_the_subject(self, catalog):
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}),
            error_response(402, "BUDGET_EXCEEDED"),
        ]
        drafts, route = _run_kind(answers)
        assert route.call_count == 2
        assert _texts(drafts) == {"meta_title": GOOD_TITLE, "meta_description": _text(195)}

    def test_failed_rewrite_leaves_the_cost_unknown(self, catalog):
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}),
            error_response(402, "BUDGET_EXCEEDED"),
        ]
        drafts, _ = _run_kind(answers)
        assert {draft.cost_usd for draft in drafts} == {None}

    def test_a_lost_lease_is_not_swallowed(self, catalog, monkeypatch):
        real = toolbox_calls.complete
        calls = []

        def complete(**kwargs):
            calls.append(kwargs["kind"])
            if len(calls) == 2:
                raise LeaseLost
            return real(**kwargs)

        monkeypatch.setattr(toolbox_calls, "complete", complete)
        with pytest.raises(LeaseLost):
            _run_kind([_answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)})])


class TestCostAndEvents:
    def test_both_calls_are_charged_to_the_task_and_shared_by_the_drafts(
        self, catalog, make_worker, django_capture_on_commit_callbacks
    ):
        task = _spawn(
            make_worker,
            django_capture_on_commit_callbacks,
            [
                _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}, "0.01"),
                _answer({"meta_description": _text(150)}, "0.004"),
            ],
        )
        proposals = ContentProposal.objects.filter(task=task)
        assert (task.cost_usd, task.cost_breakdown, task.cost_complete) == (
            Decimal("0.014"),
            {"completion": "0.01400000"},
            True,
        )
        assert [p.cost_usd for p in proposals] == [Decimal("0.007")] * 2

    def test_failed_rewrite_marks_the_task_cost_incomplete(
        self, catalog, make_worker, django_capture_on_commit_callbacks
    ):
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}, "0.01"),
            error_response(402, "BUDGET_EXCEEDED"),
        ]
        task = _spawn(make_worker, django_capture_on_commit_callbacks, answers)
        assert (task.cost_usd, task.cost_complete) == (Decimal("0.01"), False)
        assert {p.cost_usd for p in ContentProposal.objects.filter(task=task)} == {None}
        assert task.status == "done"

    def test_the_rewrite_is_logged_as_an_event(self, catalog, caplog):
        caplog.set_level(logging.INFO, logger="enrichment.queue")
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}),
            _answer({"meta_description": _text(170)}),
        ]
        _run_kind(answers)
        assert "event=field_rewrite worker=pim.writer fields=meta_description still_out=1" in caplog.text

    def test_a_failed_rewrite_is_logged_with_its_code(self, catalog, caplog):
        caplog.set_level(logging.INFO, logger="enrichment.queue")
        answers = [
            _answer({"meta_title": GOOD_TITLE, "meta_description": _text(195)}),
            error_response(402, "BUDGET_EXCEEDED"),
        ]
        _run_kind(answers)
        assert "event=field_rewrite_failed worker=pim.writer code=BUDGET_EXCEEDED" in caplog.text

    def test_in_range_values_log_nothing(self, catalog, caplog):
        caplog.set_level(logging.INFO, logger="enrichment.queue")
        _run_kind([_answer({"meta_title": GOOD_TITLE, "meta_description": _text(150)})])
        assert "field_rewrite" not in caplog.text


def _spawn(make_worker, capture, answers: list):
    make_worker(key="pim.writer", label="Writer", kind="write", options={"field_limits": LIMITS})
    with mock_toolbox() as router, capture(execute=True):
        router["complete"].mock(side_effect=answers)
        task = task_service.spawn(
            type="pim.writer", scope_spec={"mode": "list", "module": "pim", "refs": ["SKU-1"]}, params=PARAMS
        )
    task.refresh_from_db()
    return task


class TestQaLimitWarning:
    LOCATOR = {"channel": "default", "language": "pl", "feature_idx": "meta_description"}

    def _proposal(self, text: str, **over):
        fields = {
            "target_module": "pim",
            "subject_ref": "SKU-1",
            "target_kind": "attribute_value",
            "target_locator": self.LOCATOR,
            "proposed_value": {"text": text},
            "source": "worker:pim.writer@v1",
        }
        return proposal_service.intake(**{**fields, **over})

    @pytest.fixture(autouse=True)
    def _writer(self, fake_adapter, make_worker):
        make_worker(key="pim.writer", label="Writer", kind="write", options={"field_limits": LIMITS})

    def test_too_long_text_is_flagged_in_the_style_of_the_data_limits(self):
        warnings = self._proposal(_text(195)).warnings
        assert warnings == [{"code": "LIMIT", "message": "195 characters, the limit is 160.", "span": None}]

    def test_too_short_text_is_flagged(self):
        warnings = self._proposal(_text(100)).warnings
        assert warnings == [{"code": "LIMIT", "message": "100 characters, at least 120.", "span": None}]

    def test_text_in_range_has_no_warning(self):
        assert self._proposal(_text(150)).warnings == []

    def test_another_field_of_the_same_worker_uses_its_own_range(self):
        locator = {**self.LOCATOR, "feature_idx": "meta_title"}
        assert self._proposal(_text(150), target_locator=locator).warnings[0]["message"].endswith("the limit is 60.")

    def test_a_field_without_a_range_is_not_checked(self):
        locator = {**self.LOCATOR, "feature_idx": "description"}
        assert self._proposal(_text(5000), target_locator=locator).warnings == []

    def test_a_proposal_of_another_worker_or_a_person_is_not_checked(self):
        assert self._proposal(_text(195), source="worker:pim.other@v1").warnings == []
        assert self._proposal(_text(195), source="").warnings == []

    def test_an_operator_edit_is_checked_again(self, admin_user):
        proposal = self._proposal(_text(195))
        proposal_service.update_value(proposal, {"text": _text(150)}, user=admin_user)
        proposal.refresh_from_db()
        assert proposal.warnings == []
        assert [w["code"] for w in proposal.revisions.first().warnings] == ["LIMIT"]

    def test_recheck_keeps_the_warning_and_the_text_carries_no_path(self):
        proposal = qa_service.recheck(self._proposal(_text(195)))
        assert [(w["code"], w.get("path")) for w in proposal.warnings] == [("LIMIT", None)]

    def test_other_checks_still_run_next_to_the_limit(self):
        proposal = self._proposal(f"{_text(170)} 99 reasons")
        assert [w["code"] for w in proposal.warnings] == ["LIMIT", "NUMBER_NOT_IN_FACTS"]


def test_range_covers_its_ends_and_only_short_ranges_are_correctable():
    limit = FieldLimit(min=120, max=160)
    assert (limit.covers(_text(120)), limit.covers(_text(161)), limit.correctable) == (True, False, True)
    assert not FieldLimit(max=2000).correctable
