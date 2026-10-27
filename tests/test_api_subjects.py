# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""`POST subjects/resolve/` — pasted SKUs split into known (with a label) and unknown, via the adapter;
`GET subjects/pictures/` — the pictures of one item."""

import pytest
from django.core.exceptions import ObjectDoesNotExist

from django_enrichment.api.admin.throttling import LookupThrottle
from django_enrichment.api.admin.views.catalog_views import CatalogViewSet
from django_enrichment.services import catalog_service

RESOLVE = "/api/enrichment/v2/admin/subjects/resolve/"
pytestmark = pytest.mark.django_db


def _body(refs: list[str], **over) -> dict:
    return {"module": "pim", "channel": "default", "refs": refs, **over}


class TestResolveService:
    def test_splits_known_and_unknown_in_request_order(self, fake_adapter):
        fake_adapter.set_subjects({"A": "Sofa", "C": ""})
        result = catalog_service.resolve_subjects(target_module="pim", channel="default", refs=["C", "B", "A"])
        assert result == {"known": [{"ref": "C", "label": ""}, {"ref": "A", "label": "Sofa"}], "unknown": ["B"]}

    def test_trims_dedupes_and_drops_blanks(self, fake_adapter):
        fake_adapter.set_subjects({"A": "Sofa"})
        result = catalog_service.resolve_subjects(target_module="pim", channel="default", refs=[" A", "A", " ", "X"])
        assert result == {"known": [{"ref": "A", "label": "Sofa"}], "unknown": ["X"]}

    def test_unknown_channel_is_not_found(self, fake_adapter):
        with pytest.raises(ValueError, match="not found"):
            catalog_service.resolve_subjects(target_module="pim", channel="nope", refs=["A"])

    def test_adapter_without_the_extra_is_not_supported(self, fake_adapter, monkeypatch):
        monkeypatch.delattr(fake_adapter, "subject_labels")
        with pytest.raises(catalog_service.NotSupportedError):
            catalog_service.resolve_subjects(target_module="pim", channel="default", refs=["A"])


class TestResolveApi:
    def test_resolves(self, admin_client, fake_adapter):
        fake_adapter.set_subjects({"SKU-1": "Linen sofa"})
        response = admin_client.post(RESOLVE, _body(["SKU-1", "SKU-404"]), format="json")
        assert response.status_code == 200, response.json()
        assert response.json() == {"known": [{"ref": "SKU-1", "label": "Linen sofa"}], "unknown": ["SKU-404"]}

    @pytest.mark.parametrize(
        "body",
        [
            _body([]),
            _body([f"S{i}" for i in range(501)]),
            _body(["x" * 129]),
            {"module": "pim", "refs": ["A"]},
            _body(["A"], module="crm"),
        ],
        ids=["no-refs", "over-500", "long-ref", "no-channel", "unknown-module"],
    )
    def test_bad_requests_are_400(self, admin_client, fake_adapter, body):
        assert admin_client.post(RESOLVE, body, format="json").status_code == 400

    def test_five_hundred_refs_pass(self, admin_client, fake_adapter):
        response = admin_client.post(RESOLVE, _body([f"S{i}" for i in range(500)]), format="json")
        assert response.status_code == 200 and len(response.json()["unknown"]) == 500

    def test_unknown_channel_is_404(self, admin_client, fake_adapter):
        assert admin_client.post(RESOLVE, _body(["A"], channel="nope"), format="json").status_code == 404

    def test_adapter_without_the_extra_is_404_not_supported(self, admin_client, fake_adapter, monkeypatch):
        monkeypatch.delattr(fake_adapter, "subject_labels")
        response = admin_client.post(RESOLVE, _body(["A"]), format="json")
        assert response.status_code == 404 and response.json()["error"] == "NOT_SUPPORTED"

    def test_requires_admin(self, regular_client, fake_adapter):
        assert regular_client.post(RESOLVE, _body(["A"]), format="json").status_code == 403

    def test_requires_authentication(self, api_client):
        assert api_client.post(RESOLVE, _body(["A"]), format="json").status_code == 401

    def test_throttled_with_a_fallback_rate(self):
        assert LookupThrottle().get_rate() == "60/min"


PICTURES = "/api/enrichment/v2/admin/subjects/pictures/"
_PICTURES = [
    {"ref": "7", "label": "Front", "url": "/media/image/front.jpg", "is_main": True},
    {"ref": "9", "label": "", "url": "/media/image/side.jpg", "is_main": False},
]


@pytest.fixture
def list_pictures(fake_adapter, monkeypatch):
    calls = []

    def _list_pictures(*, subject_ref, channel):
        calls.append((subject_ref, channel))
        if subject_ref != "SKU-1":
            raise ObjectDoesNotExist(subject_ref)
        return [dict(picture) for picture in _PICTURES]

    monkeypatch.setattr(fake_adapter, "list_pictures", _list_pictures, raising=False)
    return calls


def _query(**over) -> dict:
    return {"module": "pim", "channel": "default", "ref": "SKU-1", **over}


class TestPicturesApi:
    def test_lists_the_items_pictures(self, admin_client, list_pictures):
        response = admin_client.get(PICTURES, _query())
        assert response.status_code == 200, response.json()
        assert response.json() == _PICTURES
        assert list_pictures == [("SKU-1", "default")]

    def test_unknown_item_is_404(self, admin_client, list_pictures):
        assert admin_client.get(PICTURES, _query(ref="SKU-404")).status_code == 404

    @pytest.mark.parametrize(
        "query",
        [_query(ref=""), {"module": "pim", "channel": "default"}, _query(ref="x" * 129), _query(module="crm")],
        ids=["blank-ref", "no-ref", "long-ref", "unknown-module"],
    )
    def test_bad_requests_are_400(self, admin_client, list_pictures, query):
        assert admin_client.get(PICTURES, query).status_code == 400

    def test_adapter_without_the_extra_is_404_not_supported(self, admin_client, fake_adapter):
        response = admin_client.get(PICTURES, _query())
        assert response.status_code == 404 and response.json()["error"] == "NOT_SUPPORTED"

    def test_requires_admin(self, regular_client, list_pictures):
        assert regular_client.get(PICTURES, _query()).status_code == 403

    def test_requires_authentication(self, api_client, list_pictures):
        assert api_client.get(PICTURES, _query()).status_code == 401

    def test_is_throttled(self):
        view = CatalogViewSet()
        view.action = "subject_pictures"
        assert [type(throttle) for throttle in view.get_throttles()] == [LookupThrottle]
