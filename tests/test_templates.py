# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Task templates (service + API), POST tasks/ with `template`, rule promotion, and the field catalog API."""

import httpx
import pytest
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.models import EnrichmentTask, TaskTemplate
from django_enrichment.services import proposal_service, revise_service, rules_service, template_service

TEMPLATES = "/api/enrichment/v2/admin/templates/"
TASKS = "/api/enrichment/v2/admin/tasks/"
FIELDS = "/api/enrichment/v2/admin/fields/"
LANGUAGES = "/api/enrichment/v2/admin/languages/"
SCOPE = {"mode": "list", "module": "pim", "refs": ["SKU-1"]}
CATALOG = [{"key": "meta_title", "label": "Meta title", "type": "text", "max_length": 60, "translatable": True}]
pytestmark = pytest.mark.django_db


@pytest.fixture
def writer(make_worker, fake_adapter):
    return make_worker(key="pim.writer", label="Writer", kind="write", mode="external", options={"rules": ["W."]})


def _template(**over) -> TaskTemplate:
    fields = {"key": "meta", "label": "Meta descriptions", "worker_key": "pim.writer"}
    fields["params"] = {"fields": ["meta_title"], "languages": ["*"], "prompt": "Be short."}
    fields["rules"] = ["Brand first."]
    return template_service.create_template(**{**fields, **over})


class TestTemplateService:
    def test_params_validated_against_the_kind_with_channel_optional(self, writer):
        assert _template().params["languages"] == ["*"]
        with pytest.raises(ValueError, match="fields"):
            _template(key="bad", params={"languages": ["pl"]})

    def test_rule_limits_and_unknown_worker(self, writer):
        with pytest.raises(ValueError, match="200 characters"):
            _template(rules=["x" * 201])
        with pytest.raises(ValueError, match="unknown worker"):
            _template(worker_key="pim.nope")

    def test_update_rejects_non_editable_fields(self, writer):
        with pytest.raises(ValueError, match="not editable"):
            template_service.update_template(_template(), {"key": "other"})

    def test_merge_request_wins_and_rules_concatenate(self, writer):
        _template()
        task_type, params = template_service.merge_into_task(
            "meta", task_type="", params={"channel": "default", "prompt": "Mine.", "rules": ["Mine."]}
        )
        assert task_type == "pim.writer"
        assert params["prompt"] == "Mine." and params["languages"] == ["*"]
        assert params["rules"] == ["Brand first.", "Mine."]

    @pytest.mark.parametrize(
        ("run_params", "preset"),
        [({"operation": "packshot"}, None), ({"operation": "enhance"}, "enhance-photo"), ({}, "enhance-photo")],
    )
    def test_merge_drops_the_preset_of_another_operation(self, make_worker, run_params, preset):
        make_worker(key="pim.image", label="Image", kind="image", mode="internal")
        _template(key="enhance", worker_key="pim.image", params={"operation": "enhance", "preset": "enhance-photo"})
        _, params = template_service.merge_into_task(
            "enhance", task_type="", params={"channel": "default", **run_params}
        )
        assert params.get("preset") == preset

    @pytest.mark.parametrize("problem", ["inactive", "unknown", "worker_inactive", "type_mismatch"])
    def test_merge_refuses(self, writer, problem):
        template = _template()
        if problem == "inactive":
            template_service.update_template(template, {"active": False})
        if problem == "worker_inactive":
            writer.active = False
            writer.save()
        key = "nope" if problem == "unknown" else "meta"
        task_type = "pim.other" if problem == "type_mismatch" else ""
        with pytest.raises(ValueError):
            template_service.merge_into_task(key, task_type=task_type, params={"channel": "default"})


class TestSpawnWithTemplate:
    def test_post_tasks_with_template_keeps_the_key(self, admin_client, writer):
        _template()
        body = {"template": "meta", "scope_spec": SCOPE, "params": {"channel": "default"}}
        response = admin_client.post(TASKS, body, format="json")
        assert response.status_code == 201, response.json()
        task = response.json()
        assert task["type"] == "pim.writer"
        assert task["params"]["template"] == "meta"
        assert task["params"]["rules"] == ["Brand first."]
        assert task["params"]["prompt"] == "Be short."

    def test_inactive_template_is_400(self, admin_client, writer):
        template_service.update_template(_template(), {"active": False})
        body = {"template": "meta", "scope_spec": SCOPE, "params": {"channel": "default"}}
        assert admin_client.post(TASKS, body, format="json").status_code == 400

    def test_neither_type_nor_template_is_400(self, admin_client, writer):
        assert admin_client.post(TASKS, {"scope_spec": SCOPE}, format="json").status_code == 400


class TestTemplateRulesInCorrections:
    def _proposal(self, admin_client, writer):
        _template()
        body = {"template": "meta", "scope_spec": SCOPE, "params": {"channel": "default"}}
        task_id = admin_client.post(TASKS, body, format="json").json()["id"]
        locator = {"channel": "default", "language": "pl", "feature_idx": "meta_title"}
        return proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator=locator,
            proposed_value={"text": "Old title"},
            task=EnrichmentTask.objects.get(pk=task_id),
            source="worker:pim.writer@v1",
        )

    def test_promote_to_template_appends_the_rule(self, admin_client, writer):
        proposal = self._proposal(admin_client, writer)
        assert rules_service.promote(proposal, "No emojis.", target="template") == ["Brand first.", "No emojis."]
        assert TaskTemplate.objects.get(key="meta").rules == ["Brand first.", "No emojis."]

    @pytest.mark.usefixtures("toolbox_settings")
    def test_correction_prompt_has_template_worker_rules_and_field_meta(self, admin_client, writer, fake_adapter):
        fake_adapter.set_catalog(CATALOG)
        proposal = self._proposal(admin_client, writer)
        parsed = {**CANNED_COMPLETION, "parsed": {"text": "New title"}}
        with mock_toolbox() as router:
            route = router["complete"].mock(return_value=httpx.Response(200, json=parsed))
            revise_service.revise(proposal, instruction="Shorter.")
            body = route.calls[0].request.read().decode()
        assert "- Brand first.\\n- W." in body
        assert '\\"label\\": \\"Meta title\\"' in body
        assert "(max 60 chars)" in body


class TestTemplateApi:
    _body = {
        "key": "pim.meta",
        "label": "Meta descriptions",
        "worker_key": "pim.writer",
        "params": {"fields": ["meta_title"], "languages": ["*"]},
        "rules": ["Brand first."],
        "position": 2,
    }

    def test_crud(self, admin_client, writer):
        created = admin_client.post(TEMPLATES, self._body, format="json")
        assert created.status_code == 201, created.json()
        assert created.json() == {
            **self._body,
            "worker_label": "Writer",
            "kind": "write",
            "target_module": "pim",
            "active": True,
        }
        url = f"{TEMPLATES}pim.meta/"
        assert admin_client.patch(url, {"active": False}, format="json").json()["active"] is False
        assert [t["key"] for t in admin_client.get(TEMPLATES, {"target_module": "pim"}).json()] == ["pim.meta"]
        assert admin_client.get(TEMPLATES, {"target_module": "crm"}).json() == []
        assert admin_client.get(url).json()["label"] == "Meta descriptions"
        assert admin_client.delete(url).status_code == 204
        assert admin_client.get(url).status_code == 404

    def test_validation_errors_are_400(self, admin_client, writer):
        assert admin_client.post(TEMPLATES, {**self._body, "key": "Bad Key"}, format="json").status_code == 400
        bad_params = {**self._body, "params": {"languages": ["pl"]}}
        assert admin_client.post(TEMPLATES, bad_params, format="json").status_code == 400
        admin_client.post(TEMPLATES, self._body, format="json")
        assert admin_client.post(TEMPLATES, self._body, format="json").status_code == 400

    def test_list_is_ordered_by_position(self, admin_client, writer):
        _template(key="b", position=1)
        _template(key="a", position=5)
        assert [t["key"] for t in admin_client.get(TEMPLATES).json()] == ["b", "a"]

    @pytest.mark.parametrize("method", ["get", "post"])
    def test_requires_admin(self, regular_client, api_client, method):
        assert getattr(regular_client, method)(TEMPLATES).status_code == 403

    def test_requires_authentication(self, api_client):
        assert api_client.get(TEMPLATES).status_code == 401


class TestCatalogApi:
    def test_fields_and_languages_pass_through(self, admin_client, fake_adapter):
        fake_adapter.set_catalog(CATALOG)
        query = {"target_module": "pim", "channel": "default", "language": "pl"}
        assert admin_client.get(FIELDS, query).json() == CATALOG
        assert admin_client.get(LANGUAGES, query).json() == ["pl", "en", "de"]

    def test_adapter_without_describe_fields_is_404_not_supported(self, admin_client, fake_adapter, monkeypatch):
        monkeypatch.delattr(fake_adapter, "describe_fields")
        response = admin_client.get(FIELDS, {"target_module": "pim", "channel": "default"})
        assert response.status_code == 404
        assert response.json()["error"] == "NOT_SUPPORTED"

    def test_missing_query_and_unknown_module_are_400(self, admin_client, fake_adapter):
        assert admin_client.get(FIELDS, {"target_module": "pim"}).status_code == 400
        assert admin_client.get(LANGUAGES, {"target_module": "crm", "channel": "x"}).status_code == 400

    def test_requires_admin(self, regular_client):
        assert regular_client.get(FIELDS, {"target_module": "pim", "channel": "default"}).status_code == 403
