# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Generic kinds `write` / `pick` and the `translate` fields override — fake adapter extras, mocked toolbox."""

import json
from types import SimpleNamespace

import httpx
import pytest
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.kinds import SubjectError
from django_enrichment.kinds.pick import PickKind
from django_enrichment.kinds.translate import TranslateKind
from django_enrichment.kinds.write import WriteKind
from django_enrichment.models import ContentProposal
from django_enrichment.services import task_service, toolbox_calls

CATALOG = [
    {"key": "meta_title", "label": "Meta title", "type": "text", "max_length": 60, "translatable": True},
    {"key": "description", "label": "Long description", "type": "html", "max_length": None, "translatable": True},
    {
        "key": "color",
        "label": "Color",
        "type": "select",
        "max_length": None,
        "translatable": False,
        "options": [{"value": "red", "label": "Red"}, {"value": "blue", "label": "Blue"}],
        "options_truncated": False,
    },
    {
        "key": "tags",
        "label": "Tags",
        "type": "multiselect",
        "max_length": None,
        "translatable": False,
        "options": [{"value": "eco", "label": "Eco"}, {"value": "new", "label": "New"}],
        "options_truncated": False,
    },
]
pytestmark = pytest.mark.usefixtures("toolbox_settings")


def _worker(**over):
    fields = {"key": "pim.x", "version": 1, "target_module": "pim", "system_prompt": "Be precise.", "model": "m"}
    return SimpleNamespace(**{**fields, **over})


def _answer(parsed: dict) -> httpx.Response:
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": parsed})


def _sent(route, index: int = 0) -> dict:
    return json.loads(route.calls[index].request.content)


@pytest.fixture
def catalog(fake_adapter):
    fake_adapter.set_catalog(CATALOG)
    return fake_adapter


class TestWrite:
    _params = {"channel": "default", "fields": ["meta_title", "description"], "languages": ["pl"]}

    def _run(self, params=None, options=None, parsed=None):
        parsed = parsed or {"meta_title": "Chair", "description": "<p>Nice</p><script>x</script>"}
        worker = _worker(options=options or {"rules": ["No prices."], "field_rules": {"meta_title": "Brand first."}})
        with mock_toolbox() as router:
            route = router["complete"].mock(return_value=_answer(parsed))
            drafts = WriteKind().run(worker, SimpleNamespace(params={**self._params, **(params or {})}), "SKU-1")
        return drafts, route

    def test_one_completion_with_multi_field_schema_and_max_length(self, catalog):
        drafts, route = self._run()
        schema = _sent(route)["json_schema"]
        assert route.call_count == 1
        assert schema["required"] == ["meta_title", "description"]
        assert schema["properties"] == {
            "meta_title": {"type": "string", "maxLength": 60},
            "description": {"type": "string"},
        }
        assert [d.target_locator["feature_idx"] for d in drafts] == ["meta_title", "description"]

    def test_html_field_is_sanitised_and_text_kept(self, catalog):
        drafts, _ = self._run()
        assert [d.proposed_value for d in drafts] == [{"text": "Chair"}, {"text": "<p>Nice</p>"}]

    def test_prompt_has_rules_labels_and_item_context_without_pim_wording(self, catalog):
        _, route = self._run(params={"prompt": "Mention the warranty.", "rules": ["Use 'you'."]})
        user = _sent(route)["messages"][1]["content"]
        assert "- No prices.\n- Use 'you'." in user
        assert "Mention the warranty." in user
        assert "Meta title" in user and "at most 60 characters" in user and "Brand first." in user
        assert '<item_context>\n{"sku": "SKU-1"' in user
        assert "product" not in user.lower()

    def test_star_languages_expand_to_channel_languages(self, catalog):
        drafts, route = self._run(params={"languages": ["*"]})
        assert route.call_count == 3
        assert [d.target_locator["language"] for d in drafts[::2]] == ["pl", "en", "de"]

    def test_missing_extras_fall_back(self, fake_adapter, monkeypatch):
        for name in ("describe_fields", "make_locator", "build_context"):
            monkeypatch.delattr(fake_adapter, name)
        drafts, route = self._run(parsed={"meta_title": "T", "description": "<p>x</p><script>y</script>"})
        assert _sent(route)["json_schema"]["properties"]["meta_title"] == {"type": "string"}
        assert drafts[0].target_locator == {"channel": "default", "language": "pl", "field": "meta_title"}
        assert drafts[1].proposed_value == {"text": "<p>x</p><script>y</script>"}  # no catalog → not html

    def test_star_without_channel_languages_is_a_subject_error(self, fake_adapter, monkeypatch):
        monkeypatch.delattr(fake_adapter, "channel_languages")
        with pytest.raises(SubjectError) as exc:
            self._run(params={"languages": ["*"]})
        assert exc.value.code == "NOT_SUPPORTED"

    def test_select_field_is_a_field_type_error(self, catalog):
        with pytest.raises(SubjectError) as exc:
            self._run(params={"fields": ["color"]})
        assert toolbox_calls.failure_code(exc.value) == "FIELD_TYPE"


class TestPick:
    _params = {"channel": "default", "fields": ["color", "tags"]}

    def _run(self, params=None, parsed=None):
        worker = _worker(options={"rules": ["Be strict."]})
        with mock_toolbox() as router:
            route = router["complete"].mock(return_value=_answer(parsed or {"color": "blue", "tags": ["eco", "new"]}))
            drafts = PickKind().run(worker, SimpleNamespace(params={**self._params, **(params or {})}), "SKU-1")
        return drafts, route

    def test_enum_schema_and_multiselect_array(self, catalog):
        _, route = self._run()
        properties = _sent(route)["json_schema"]["properties"]
        assert properties["color"] == {"type": "string", "enum": ["red", "blue"]}
        assert properties["tags"]["type"] == "array"
        assert properties["tags"]["items"] == {"type": "string", "enum": ["eco", "new"]}

    def test_one_completion_one_draft_per_field_language_none(self, catalog):
        drafts, route = self._run()
        assert route.call_count == 1
        assert [(d.target_locator, d.proposed_value) for d in drafts] == [
            ({"channel": "default", "language": None, "feature_idx": "color"}, {"option": "blue"}),
            ({"channel": "default", "language": None, "feature_idx": "tags"}, {"options": ["eco", "new"]}),
        ]

    def test_prompt_lists_options_and_main_language_context(self, catalog):
        _, route = self._run()
        user = _sent(route)["messages"][1]["content"]
        assert "red (Red), blue (Blue)" in user and "- Be strict." in user
        assert '"language": "pl"' in user

    def test_text_field_is_a_field_type_error_without_a_call(self, catalog):
        with mock_toolbox() as router:
            with pytest.raises(SubjectError) as exc:
                PickKind().run(
                    _worker(options={}), SimpleNamespace(params={**self._params, "fields": ["meta_title"]}), "S"
                )
            assert not router["complete"].called
        assert exc.value.code == "FIELD_TYPE"

    def test_no_catalog_is_a_field_type_error(self, fake_adapter, monkeypatch):
        monkeypatch.delattr(fake_adapter, "describe_fields")
        with pytest.raises(SubjectError, match="color"):
            self._run()


class TestTranslateFieldsOverride:
    URL = "http://toolbox.test/api/ai-translator/v2/admin/shop/translate/"

    def test_params_fields_override_worker_fields(self, fake_adapter):
        locator = {"channel": "default", "language": "pl", "feature_idx": "name"}
        fake_adapter.set_current("SKU-1", "attribute_value", locator, {"text": "Krzesło"})
        items = [{"key": "name", "source_text": "Krzesło", "translated_text": "Chair", "char_count": 1}]
        params = {"channel": "default", "source_language": "pl", "target_languages": ["en"], "fields": ["name"]}
        with mock_toolbox() as router:
            route = router.post(self.URL).mock(return_value=httpx.Response(200, json={"items": items}))
            drafts = TranslateKind().run(
                _worker(options={"fields": ["description"]}), SimpleNamespace(params=params), "SKU-1"
            )
        assert _sent(route)["items"] == [{"key": "name", "text": "Krzesło"}]
        assert drafts[0].proposed_value == {"text": "Chair"}


@pytest.mark.django_db
def test_runner_records_field_type_and_writes_pick_proposals(catalog, make_worker, django_capture_on_commit_callbacks):
    make_worker(key="pim.tagger", label="Tagger", kind="pick", options={})
    with mock_toolbox() as router, django_capture_on_commit_callbacks(execute=True):
        router["complete"].mock(return_value=_answer({"color": "red"}))
        task = task_service.spawn(
            type="pim.tagger",
            scope_spec={"mode": "list", "module": "pim", "refs": ["SKU-1"]},
            params={"channel": "default", "fields": ["color"]},
        )
        bad = task_service.spawn(
            type="pim.tagger",
            scope_spec={"mode": "list", "module": "pim", "refs": ["SKU-2"]},
            params={"channel": "default", "fields": ["meta_title"]},
        )
    task.refresh_from_db()
    bad.refresh_from_db()
    assert ContentProposal.objects.get(task=task).proposed_value == {"option": "red"}
    assert (bad.status, bad.errors[0]["code"]) == ("failed", "FIELD_TYPE")
