# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API tests for spawn rules (etap-13) — CRUD, auth gating, the run endpoint's three
statuses, and the batch_key filter on the tasks list."""

import pytest

from django_enrichment.models import EnrichmentTask, SpawnRule
from django_enrichment.services import spawn_rule_service, task_service

RULES = "/api/enrichment/v2/admin/spawn-rules/"
TASKS = "/api/enrichment/v2/admin/tasks/"


def _payload(**over):
    payload = {
        "key": "desc-pl",
        "module": "pim",
        "check_key": "pl-description",
        "scope": {"candidates": [{"subject_ref": "S1", "target_kind": "attribute_value"}]},
        "task_type": "fix-attribute",
        "task_params": {"feature": "description"},
    }
    payload.update(over)
    return payload


# ── auth ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_unauthenticated_returns_401(api_client):
    assert api_client.get(RULES).status_code == 401


@pytest.mark.django_db
def test_regular_user_returns_403(regular_client):
    assert regular_client.get(RULES).status_code == 403
    assert regular_client.post(RULES, _payload(), format="json").status_code == 403


# ── CRUD ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_create_returns_201(admin_client):
    response = admin_client.post(RULES, _payload(), format="json")

    assert response.status_code == 201
    body = response.json()
    assert body["key"] == "desc-pl"
    assert body["limit"] is None
    assert body["auto"] is False


@pytest.mark.django_db
def test_create_invalid_key_returns_400(admin_client):
    assert admin_client.post(RULES, _payload(key="Bad Key!"), format="json").status_code == 400


@pytest.mark.django_db
def test_create_duplicate_key_returns_400(admin_client):
    admin_client.post(RULES, _payload(), format="json")
    assert admin_client.post(RULES, _payload(), format="json").status_code == 400


@pytest.mark.django_db
def test_list_returns_pagination_envelope_and_filters(admin_client):
    admin_client.post(RULES, _payload(), format="json")
    admin_client.post(RULES, _payload(key="other", active=False), format="json")

    body = admin_client.get(RULES).json()
    assert set(body.keys()) >= {"count", "next", "previous", "results"}
    assert body["count"] == 2

    active_only = admin_client.get(RULES, {"active": "true"}).json()
    assert [r["key"] for r in active_only["results"]] == ["desc-pl"]


@pytest.mark.django_db
def test_retrieve_and_404(admin_client):
    admin_client.post(RULES, _payload(), format="json")
    assert admin_client.get(f"{RULES}desc-pl/").status_code == 200
    assert admin_client.get(f"{RULES}nope/").status_code == 404


@pytest.mark.django_db
def test_patch_updates_editable_fields(admin_client):
    admin_client.post(RULES, _payload(), format="json")

    response = admin_client.patch(f"{RULES}desc-pl/", {"auto": True, "limit": 50}, format="json")

    assert response.status_code == 200
    assert response.json()["auto"] is True
    assert response.json()["limit"] == 50


@pytest.mark.django_db
def test_patch_key_is_rejected(admin_client):
    admin_client.post(RULES, _payload(), format="json")
    # `key` is absent from SpawnRuleUpdateRequest (pydantic drops unknown fields), so a PATCH
    # smuggling it must leave the identifier untouched.
    response = admin_client.patch(f"{RULES}desc-pl/", {"key": "new-key", "auto": True}, format="json")
    assert response.status_code == 200
    assert response.json()["key"] == "desc-pl"
    assert SpawnRule.objects.filter(key="desc-pl").exists()


@pytest.mark.django_db
def test_delete_returns_200_then_404(admin_client):
    admin_client.post(RULES, _payload(), format="json")
    assert admin_client.delete(f"{RULES}desc-pl/").status_code == 200
    assert admin_client.delete(f"{RULES}desc-pl/").status_code == 404


# ── run ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_run_spawns_task_201(admin_client, fake_adapter):
    admin_client.post(RULES, _payload(), format="json")

    response = admin_client.post(f"{RULES}desc-pl/run/")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "spawned"
    assert body["task"]["batch_key"] == "gaprule:desc-pl"
    assert EnrichmentTask.objects.count() == 1


@pytest.mark.django_db
def test_run_already_running_200(admin_client, fake_adapter):
    admin_client.post(RULES, _payload(), format="json")
    first = admin_client.post(f"{RULES}desc-pl/run/").json()

    response = admin_client.post(f"{RULES}desc-pl/run/")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "already_running"
    assert body["task"]["id"] == first["task"]["id"]


@pytest.mark.django_db
def test_run_no_candidates_200(admin_client, fake_adapter):
    admin_client.post(RULES, _payload(scope={"candidates": []}), format="json")

    response = admin_client.post(f"{RULES}desc-pl/run/")

    assert response.status_code == 200
    assert response.json() == {"status": "no_candidates", "task": None}


@pytest.mark.django_db
def test_run_unknown_rule_404(admin_client):
    assert admin_client.post(f"{RULES}nope/run/").status_code == 404


@pytest.mark.django_db
def test_run_throttle_is_wired():
    from django_enrichment.api.admin.throttling import SpawnThrottle
    from django_enrichment.api.admin.views.spawn_rule_views import SpawnRuleViewSet

    view = SpawnRuleViewSet()
    view.action = "run"
    assert [type(t) for t in view.get_throttles()] == [SpawnThrottle]
    view.action = "list"
    assert view.get_throttles() == []


# ── tasks list batch_key filter (CMS running indicator) ────────────────────────────────────


@pytest.mark.django_db
def test_tasks_list_filters_by_batch_key(admin_client, fake_adapter):
    rule = spawn_rule_service.create_rule(
        key="r1",
        module="pim",
        check_key="c",
        task_type="fix-attribute",
        scope={"candidates": [{"subject_ref": "S1", "target_kind": "attribute_value"}]},
    )
    spawn_rule_service.run_rule(rule)
    task_service.spawn(type="x", scope_spec={"mode": "list", "module": "pim", "refs": ["A"]})

    body = admin_client.get(TASKS, {"batch_key": "gaprule:r1"}).json()

    assert body["count"] == 1
    assert body["results"][0]["batch_key"] == "gaprule:r1"
