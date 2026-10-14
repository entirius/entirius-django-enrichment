# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Base kinds `translate` / `field` against the fake adapter and a mocked toolbox (no network)."""

import json
from types import SimpleNamespace

import httpx
import pytest
from django_utils.toolbox import ToolboxBudgetExceededError, ToolboxError
from django_utils.toolbox.testing import CANNED_COMPLETION, error_response, mock_toolbox

from django_enrichment.kinds.field import FieldKind
from django_enrichment.kinds.translate import TranslateKind
from django_enrichment.services import toolbox_calls

TRANSLATE_URL = "http://toolbox.test/api/ai-translator/v2/admin/shop/translate/"
_PL = {"channel": "default", "language": "pl"}


def _worker(**over):
    fields = {"key": "pim.x", "version": 1, "target_module": "pim", "system_prompt": "Be precise.", "model": ""}
    return SimpleNamespace(**{**fields, **over})


def _translated(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    items = [
        {
            "key": i["key"],
            "source_text": i["text"],
            "translated_text": f"{body['target_language']}:{i['text']}",
            "char_count": 1,
        }
        for i in body["items"]
    ]
    return httpx.Response(200, json={"items": items, "total_chars": 1, "provider": "deepl"})


def _complete_answer(value: str) -> httpx.Response:
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": {"value": value}})


@pytest.mark.usefixtures("toolbox_settings")
class TestTranslate:
    def _run(self, fake_adapter, router, **params):
        fake_adapter.set_current("SKU-1", "attribute_value", {**_PL, "feature_idx": "description"}, {"text": "Opis"})
        route = router.post(TRANSLATE_URL).mock(side_effect=_translated)
        worker = _worker(options={"fields": ["description", "short_description"], "provider": "deepl"})
        task_params = {"channel": "default", "source_language": "pl", "target_languages": ["en", "de"], **params}
        drafts = TranslateKind().run(worker, SimpleNamespace(params=task_params), "SKU-1")
        return drafts, route

    def test_one_draft_per_field_and_language_empty_source_skipped(self, fake_adapter):
        with mock_toolbox() as router:
            drafts, route = self._run(fake_adapter, router)
        assert route.call_count == 2
        assert [(d.target_locator["language"], d.proposed_value) for d in drafts] == [
            ("en", {"text": "en:Opis"}),
            ("de", {"text": "de:Opis"}),
        ]
        assert drafts[0].target_locator == {"channel": "default", "language": "en", "feature_idx": "description"}

    def test_payload_carries_layered_context(self, fake_adapter):
        with mock_toolbox() as router:
            _, route = self._run(fake_adapter, router, prompt="Keep brand names.")
        body = json.loads(route.calls[0].request.content)
        assert body["context"] == "Be precise.\n\nKeep brand names."
        assert body["context_mode"] is True
        assert body["provider"] == "deepl"
        assert "formality" not in body
        assert body["items"] == [{"key": "description", "text": "Opis"}]

    def test_no_source_text_makes_no_call(self, fake_adapter):
        with mock_toolbox() as router:
            route = router.post(TRANSLATE_URL).mock(side_effect=_translated)
            worker = _worker(options={"fields": ["description"]})
            params = {"channel": "default", "source_language": "pl", "target_languages": ["en"]}
            assert TranslateKind().run(worker, SimpleNamespace(params=params), "SKU-1") == []
        assert route.call_count == 0


@pytest.mark.usefixtures("toolbox_settings")
class TestField:
    _params = {"channel": "default", "languages": ["pl", "en"], "prompt": "Mention the warranty."}

    def _run(self, options, model=""):
        worker = _worker(options=options, model=model)
        return FieldKind().run(worker, SimpleNamespace(params=self._params), "SKU-1")

    def test_one_draft_per_language_with_blank_model_from_catalogue(self, fake_adapter):
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_complete_answer("Great chair."))
            drafts = self._run({"field": "description"})
            assert router["models"].called
        assert [d.target_locator["language"] for d in drafts] == ["pl", "en"]
        assert drafts[0].proposed_value == {"text": "Great chair."}
        assert drafts[0].target_kind == "attribute_value"

    def test_request_layers_and_schema(self, fake_adapter, monkeypatch):
        monkeypatch.setattr(fake_adapter, "build_context", lambda **kw: {"sku": kw["subject_ref"]}, raising=False)
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_complete_answer("x"))
            self._run({"field": "meta_description", "max_length": 160}, model="claude-haiku")
            body = json.loads(router["complete"].calls[0].request.content)
            assert not router["models"].called
        assert body["model"] == "claude-haiku"
        assert body["messages"][0] == {"role": "system", "content": "Be precise."}
        user = body["messages"][1]["content"]
        assert user.startswith("Mention the warranty.")
        assert '{"sku": "SKU-1"}' in user
        assert "meta_description" in user and "160" in user
        assert body["json_schema"]["properties"]["value"] == {"type": "string", "maxLength": 160}

    def test_html_is_sanitised(self, fake_adapter):
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_complete_answer("<p>Hi</p><script>alert(1)</script>"))
            drafts = self._run({"field": "description", "format": "html"})
        assert drafts[0].proposed_value == {"text": "<p>Hi</p>"}

    def test_html_strips_links_and_images(self, fake_adapter):
        html = '<p><a href="https://x.test">Hi</a><img src="https://x.test/p.gif"></p>'
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_complete_answer(html))
            drafts = self._run({"field": "description", "format": "html"})
        assert drafts[0].proposed_value == {"text": "<p>Hi</p>"}

    def test_html_escaped_by_model_is_unescaped_then_sanitised(self, fake_adapter):
        escaped = "&lt;p&gt;Hi&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;"
        with mock_toolbox() as router:
            router["complete"].mock(return_value=_complete_answer(escaped))
            drafts = self._run({"field": "description", "format": "html"})
        assert drafts[0].proposed_value == {"text": "<p>Hi</p>"}

    def test_toolbox_error_propagates(self, fake_adapter):
        with mock_toolbox() as router:
            router["complete"].mock(return_value=error_response(402, "BUDGET_EXCEEDED"))
            with pytest.raises(ToolboxBudgetExceededError):
                self._run({"field": "description"})


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ToolboxError(400, "x", code="TRANSLATOR_NOT_CONFIGURED"), "TRANSLATOR_NOT_CONFIGURED"),
        (ToolboxError(502, "x"), "TOOLBOX_HTTP_502"),
        (TimeoutError("slow"), "TIMEOUT"),
        (ValueError("internal detail"), "INTERNAL_ERROR"),
    ],
)
def test_failure_code_is_stable_and_textless(exc, code):
    assert toolbox_calls.failure_code(exc) == code
