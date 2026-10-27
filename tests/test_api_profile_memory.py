# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — company profile and memory (contract A4 § 2, § 3)."""

import pytest

from django_enrichment.models import MemoryEntry
from django_enrichment.services import memory_service, profile_service, proposal_service

BASE = "/api/enrichment/v2/admin"
pytestmark = pytest.mark.django_db
DATA = {"brand": {"differentiators": ["Made to order"]}, "always_rules": ["No prices."]}


def _entry(**over) -> MemoryEntry:
    fields = {"kind": "rule_suggestion", "title": "No emojis.", "text": "No emojis."}
    return memory_service.remember(MemoryEntry(**{**fields, **over}))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "profile/"),
        ("put", "profile/"),
        ("get", "profile/versions/"),
        ("post", "profile/versions/1/restore/"),
        ("get", "profile/status/"),
        ("get", "profile/schema/"),
        ("get", "memory/"),
        ("patch", "memory/1/"),
        ("delete", "memory/1/"),
        ("post", "memory/1/promote/"),
    ],
)
def test_anonymous_401_and_non_admin_403(api_client, regular_user, method, path):
    assert getattr(api_client, method)(f"{BASE}/{path}", {}, format="json").status_code == 401
    api_client.force_authenticate(user=regular_user)
    assert getattr(api_client, method)(f"{BASE}/{path}", {}, format="json").status_code == 403


class TestProfile:
    def test_get_empty_then_put_then_get(self, admin_client, admin_user):
        assert admin_client.get(f"{BASE}/profile/").json()["version"] == 0
        resp = admin_client.put(f"{BASE}/profile/?channel=shop", {"data": DATA, "note": "first"}, format="json")
        assert resp.status_code == 200
        body = admin_client.get(f"{BASE}/profile/?channel=shop").json()
        assert (body["channel"], body["data"], body["version"], body["updated_by_id"]) == (
            "shop",
            DATA,
            1,
            admin_user.pk,
        )

    def test_schema_errors_are_400_with_paths(self, admin_client):
        data = {"brand": {"differentiators": [1]}, "facts": [{"id": "f1"}]}
        resp = admin_client.put(f"{BASE}/profile/", {"data": data}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"] == "VALIDATION_ERROR"
        assert {d["field"] for d in resp.json()["details"]} == {"data.brand.differentiators.0", "data.facts.0"}

    def test_body_without_data_is_400(self, admin_client):
        assert admin_client.put(f"{BASE}/profile/", {"note": "x"}, format="json").status_code == 400

    def test_versions_and_restore(self, admin_client):
        profile_service.save_profile(channel="", data={"always_rules": ["A"]}, note="a")
        profile_service.save_profile(channel="", data={"always_rules": ["B"]}, note="b")
        versions = admin_client.get(f"{BASE}/profile/versions/").json()
        assert [(v["version"], v["note"]) for v in versions] == [(2, "b"), (1, "a")]
        resp = admin_client.post(f"{BASE}/profile/versions/1/restore/")
        assert (resp.status_code, resp.json()["version"], resp.json()["data"]) == (200, 3, {"always_rules": ["A"]})
        assert admin_client.post(f"{BASE}/profile/versions/9/restore/").status_code == 404

    def test_status_and_schema(self, admin_client):
        profile_service.save_profile(channel="", data=DATA)
        status = admin_client.get(f"{BASE}/profile/status/?channel=shop").json()
        assert status["channel"] == "shop" and status["complete"] is False
        assert {s["key"]: s["complete"] for s in status["sections"]}["brand"] is True
        schema = admin_client.get(f"{BASE}/profile/schema/").json()
        assert schema["schema_version"] == 1 and "brand" in schema["json_schema"]["properties"]

    def test_channel_too_long_is_400(self, admin_client):
        assert admin_client.get(f"{BASE}/profile/?channel={'x' * 65}").status_code == 400


class TestMemory:
    def test_list_with_filters(self, admin_client):
        _entry(channel="shop")
        _entry(kind="answer", title="Since when?", text="1999")
        resp = admin_client.get(f"{BASE}/memory/?kind=answer")
        assert resp.json()["count"] == 1 and resp.json()["results"][0]["title"] == "Since when?"
        assert admin_client.get(f"{BASE}/memory/?channel=shop").json()["count"] == 1

    def test_patch_status_and_delete(self, admin_client):
        entry = _entry()
        resp = admin_client.patch(f"{BASE}/memory/{entry.pk}/", {"status": "dismissed"}, format="json")
        assert resp.json()["status"] == "dismissed"
        assert (
            admin_client.patch(f"{BASE}/memory/{entry.pk}/", {"status": "promoted"}, format="json").status_code == 400
        )
        assert admin_client.delete(f"{BASE}/memory/{entry.pk}/").status_code == 204
        assert admin_client.delete(f"{BASE}/memory/{entry.pk}/").status_code == 404

    def test_promote_creates_a_profile_version(self, admin_client):
        entry = _entry()
        resp = admin_client.post(f"{BASE}/memory/{entry.pk}/promote/", {}, format="json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["entry"]["status"] == "promoted"
        assert (body["profile"]["version"], body["profile"]["data"]) == (1, {"always_rules": ["No emojis."]})
        assert admin_client.post(f"{BASE}/memory/{entry.pk}/promote/", {}, format="json").status_code == 400

    def test_promote_fact_with_wrong_target_is_400(self, admin_client):
        entry = _entry(kind="fact_suggestion")
        resp = admin_client.post(f"{BASE}/memory/{entry.pk}/promote/", {"target": "rules"}, format="json")
        assert resp.status_code == 400

    def test_rule_promotion_to_profile_lands_in_memory(self, admin_client, fake_adapter):
        proposal = proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator={"channel": "", "language": "pl", "feature_idx": "description"},
            proposed_value={"text": "Copy."},
        )
        url = f"{BASE}/proposals/{proposal.pk}/rules/promote/"
        resp = admin_client.post(url, {"rule": "No emojis.", "target": "profile"}, format="json")
        assert resp.json() == {"target": "profile", "rules": ["No emojis."]}
        listed = admin_client.get(f"{BASE}/memory/?kind=rule_suggestion").json()["results"]
        assert listed[0]["source"] == f"proposal:{proposal.pk}"
