# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API for the AI workers: GET workers/, params validation on POST tasks/, PATCH proposals/{id}/."""

import pytest

from django_enrichment.enums import ProposalStatus
from django_enrichment.services import proposal_service

WORKERS = "/api/enrichment/v2/admin/workers/"
TASKS = "/api/enrichment/v2/admin/tasks/"
PROPOSALS = "/api/enrichment/v2/admin/proposals/"

_LOCATOR = {"channel": "default", "language": "pl", "feature_idx": "description"}


@pytest.mark.django_db
class TestWorkers:
    def test_lists_active_workers_with_params_schema(self, admin_client, make_worker):
        make_worker()
        make_worker(key="pim.description", kind="field", options={"field": "description"})
        make_worker(key="pim.off", active=False)
        make_worker(key="crm.x", target_module="crm")
        response = admin_client.get(WORKERS, {"target_module": "pim"})
        assert response.status_code == 200
        body = response.json()
        assert [w["key"] for w in body] == ["pim.description", "pim.translate"]
        assert set(body[0]) == {"key", "label", "kind", "target_module", "mode", "params_schema"}
        assert "languages" in body[0]["params_schema"]["properties"]

    def test_without_filter_lists_every_module(self, admin_client, make_worker):
        make_worker()
        make_worker(key="crm.x", target_module="crm")
        assert len(admin_client.get(WORKERS).json()) == 2

    def test_requires_admin(self, regular_client):
        assert regular_client.get(WORKERS).status_code == 403


@pytest.mark.django_db
class TestTaskParams:
    def _post(self, client, params):
        scope_spec = {"mode": "list", "module": "pim", "refs": ["SKU-1"]}
        return client.post(TASKS, {"type": "pim.translate", "scope_spec": scope_spec, "params": params}, format="json")

    def test_invalid_params_for_worker_is_400(self, admin_client, make_worker, fake_adapter):
        make_worker(mode="external")
        response = self._post(admin_client, {"channel": "default"})
        assert response.status_code == 400
        assert response.json()["error"] == "VALIDATION_ERROR"

    def test_valid_params_are_stored_normalised(self, admin_client, make_worker, fake_adapter):
        make_worker(mode="external")
        response = self._post(admin_client, {"channel": "default", "source_language": "pl", "target_languages": ["en"]})
        assert response.status_code == 201
        assert response.json()["params"]["prompt"] == ""


@pytest.mark.django_db
class TestProposalPatch:
    def _proposal(self):
        return proposal_service.intake(
            target_module="pim",
            subject_ref="SKU-1",
            target_kind="attribute_value",
            target_locator=_LOCATOR,
            proposed_value={"text": "AI copy."},
        )

    def test_pending_value_is_replaced(self, admin_client, fake_adapter):
        proposal = self._proposal()
        response = admin_client.patch(
            f"{PROPOSALS}{proposal.pk}/", {"proposed_value": {"text": "Edited."}}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["proposed_value"] == {"text": "Edited."}
        assert response.json()["status"] == ProposalStatus.PENDING.value

    def test_non_pending_is_400(self, admin_client, fake_adapter):
        proposal = proposal_service.reject(self._proposal(), "no")
        response = admin_client.patch(f"{PROPOSALS}{proposal.pk}/", {"proposed_value": {"text": "x"}}, format="json")
        assert response.status_code == 400
        proposal.refresh_from_db()
        assert proposal.proposed_value == {"text": "AI copy."}

    def test_missing_value_is_400(self, admin_client, fake_adapter):
        proposal = self._proposal()
        assert admin_client.patch(f"{PROPOSALS}{proposal.pk}/", {}, format="json").status_code == 400

    def test_unknown_proposal_is_404(self, admin_client):
        assert admin_client.patch(f"{PROPOSALS}999/", {"proposed_value": {}}, format="json").status_code == 404

    def test_requires_admin(self, regular_client):
        assert regular_client.patch(f"{PROPOSALS}1/", {"proposed_value": {}}, format="json").status_code == 403
