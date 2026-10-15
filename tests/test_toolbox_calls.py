# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The toolbox door: the provider-portable schema and the operator-facing failure detail."""

import json

import httpx
import pytest
from django_utils.toolbox import ToolboxError
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.models import EnrichmentTask
from django_enrichment.services import runner_service, toolbox_calls

KEYWORDS_LIKE = {
    "type": "object",
    "required": ["primary", "format"],
    "properties": {
        "primary": {"type": "string", "minLength": 1, "maxLength": 120, "description": "Main phrase"},
        "format": {"type": "string", "pattern": "^a"},
        "tags": {"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 2, "uniqueItems": True},
        "volume": {"type": "integer", "minimum": 0},
        "intent": {"enum": ["a", "b"], "type": "string"},
    },
    "additionalProperties": False,
}


class TestProviderSchema:
    def test_limits_become_hints_and_keywords_are_dropped(self):
        schema = toolbox_calls.provider_schema(KEYWORDS_LIKE)
        props = schema["properties"]
        assert props["primary"] == {
            "type": "string",
            "description": "Main phrase (min 1 chars, max 120 chars)",
        }
        assert props["tags"] == {
            "type": "array",
            "items": {"type": "string", "description": "(max 20 chars)"},
            "description": "(max 2 items)",
        }
        assert props["volume"] == {"type": "integer", "description": "(min 0)"}

    def test_structure_and_property_names_survive(self):
        schema = toolbox_calls.provider_schema(KEYWORDS_LIKE)
        assert schema["required"] == ["primary", "format"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["format"] == {"type": "string"}
        assert schema["properties"]["intent"]["enum"] == ["a", "b"]

    def test_input_is_not_mutated(self):
        before = json.dumps(KEYWORDS_LIKE, sort_keys=True)
        toolbox_calls.provider_schema(KEYWORDS_LIKE)
        assert json.dumps(KEYWORDS_LIKE, sort_keys=True) == before

    @pytest.mark.django_db
    @pytest.mark.usefixtures("toolbox_settings")
    def test_complete_sends_the_portable_schema(self):
        with mock_toolbox() as router:
            route = router["complete"].mock(
                return_value=httpx.Response(200, json={**CANNED_COMPLETION, "parsed": {"primary": "x"}})
            )
            toolbox_calls.complete(system="", user="u", json_schema=KEYWORDS_LIKE, model="m", kind="generate")
        sent = json.loads(route.calls.last.request.content)["json_schema"]
        assert "maxLength" not in json.dumps(sent)
        assert sent["properties"]["primary"]["description"] == "Main phrase (min 1 chars, max 120 chars)"


class TestFailureDetail:
    def test_toolbox_message_is_carried(self):
        exc = ToolboxError(502, "Bad Gateway", "PROVIDER_ERROR", upstream_message="schema   rejected\nby provider")
        assert toolbox_calls.failure_detail(exc) == "HTTP 502 PROVIDER_ERROR: schema rejected by provider"

    def test_blank_message_is_said_explicitly(self):
        detail = toolbox_calls.failure_detail(ToolboxError(502, "Bad Gateway"))
        assert detail.startswith("HTTP 502 -: no message")

    def test_other_errors_have_no_detail(self):
        assert toolbox_calls.failure_detail(ValueError("secret path /etc")) == ""

    def test_detail_is_capped(self):
        exc = ToolboxError(502, "x", "C", upstream_message="m" * 1000)
        assert len(toolbox_calls.failure_detail(exc)) == 300

    @pytest.mark.django_db
    def test_run_subject_reports_detail(self):
        class Boom:
            def run(self, *_args):
                raise ToolboxError(502, "Bad Gateway", "PROVIDER_ERROR", upstream_message="rejected")

        worker = type("W", (), {"key": "k", "target_module": "enrichment"})()
        task = EnrichmentTask.objects.create(type="t")
        result = runner_service.run_subject(worker, Boom(), task, "run:1")
        assert (result.error_code, result.error_message) == ("PROVIDER_ERROR", "HTTP 502 PROVIDER_ERROR: rejected")


class Flaky:
    """Fails with an edge 502 (no toolbox code) `failures` times, then returns no drafts."""

    def __init__(self, failures: int):
        self.calls, self.failures = 0, failures

    def run(self, *_args):
        self.calls += 1
        if self.calls <= self.failures:
            raise ToolboxError(502, "Bad Gateway", "")
        return []


@pytest.mark.django_db
class TestTransientRetry:
    def _run(self, kind):
        worker = type("W", (), {"key": "k", "target_module": "enrichment"})()
        task = EnrichmentTask.objects.create(type="t")
        return runner_service.run_subject(worker, kind, task, "run:1")

    def test_an_edge_502_is_retried_once(self):
        kind = Flaky(failures=1)
        result = self._run(kind)
        assert (kind.calls, result.error_code) == (2, "")

    def test_a_second_502_counts_as_failed(self):
        kind = Flaky(failures=5)
        result = self._run(kind)
        assert (kind.calls, result.error_code) == (2, "TOOLBOX_HTTP_502")

    def test_retries_off(self, settings):
        settings.ENRICHMENT_TRANSIENT_RETRIES = 0
        kind = Flaky(failures=1)
        assert self._run(kind).error_code == "TOOLBOX_HTTP_502"
        assert kind.calls == 1
