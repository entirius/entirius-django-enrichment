# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API tests (etap-05) — happy paths, auth gating, v2 error envelope, idempotency, bulk.

The in-memory `fake_adapter` (registered under "pim") lets intake/accept run the full bus without
django-pim — the real PIM apply is covered in django-pim's `test_enrichment_adapter.py`.
"""

import pytest

from django_enrichment.enums import ProposalStatus, TaskStatus
from django_enrichment.models import ContentProposal

TASKS = "/api/enrichment/v2/admin/tasks/"
PROPOSALS = "/api/enrichment/v2/admin/proposals/"

_LOCATOR = {"channel": "default", "language": "pl", "feature_idx": "description"}


def _spawn_payload(**over):
    payload = {
        "type": "fix-attribute",
        "scope_spec": {"mode": "list", "module": "pim", "channel": "default", "refs": ["SKU-1"]},
        "params": {"feature": "description"},
    }
    payload.update(over)
    return payload


def _intake_payload(**over):
    payload = {
        "target_module": "pim",
        "subject_ref": "SKU-1",
        "target_kind": "attribute_value",
        "target_locator": _LOCATOR,
        "proposed_value": {"text": "Better copy."},
        "source": "n8n:test",
    }
    payload.update(over)
    return payload


# --- Auth gating ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuth:
    def test_list_tasks_401_without_auth(self, api_client):
        resp = api_client.get(TASKS)
        assert resp.status_code == 401
        assert resp.json()["error"] == "AUTHENTICATION_REQUIRED"

    def test_list_tasks_403_for_non_admin(self, regular_client):
        resp = regular_client.get(TASKS)
        assert resp.status_code == 403
        assert resp.json()["error"] == "PERMISSION_DENIED"

    def test_list_tasks_200_for_admin(self, admin_client):
        resp = admin_client.get(TASKS)
        assert resp.status_code == 200
        assert set(resp.json()) == {"count", "next", "previous", "results"}


# --- Tasks ---------------------------------------------------------------------------------


@pytest.mark.django_db
class TestTasks:
    def test_spawn_returns_201_and_open_task(self, admin_client):
        resp = admin_client.post(TASKS, _spawn_payload(), format="json")
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == TaskStatus.OPEN.value
        assert body["type"] == "fix-attribute"
        assert body["requested_by_id"] is not None

    def test_spawn_invalid_scope_spec_returns_v2_envelope(self, admin_client):
        resp = admin_client.post(TASKS, _spawn_payload(scope_spec={"mode": "list"}), format="json")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "VALIDATION_ERROR"
        assert "debug_id" in body

    def test_spawn_missing_type_is_validation_error(self, admin_client):
        payload = _spawn_payload()
        del payload["type"]
        resp = admin_client.post(TASKS, payload, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"] == "VALIDATION_ERROR"

    def test_list_filters_by_status(self, admin_client):
        admin_client.post(TASKS, _spawn_payload(), format="json")
        resp = admin_client.get(TASKS, {"status": TaskStatus.OPEN.value})
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    def test_targets_resolves_via_adapter(self, admin_client, fake_adapter):
        task_id = admin_client.post(TASKS, _spawn_payload(), format="json").json()["id"]
        resp = admin_client.get(f"{TASKS}{task_id}/targets/")
        assert resp.status_code == 200
        assert resp.json()["targets"] == ["SKU-1"]

    def test_patch_status_drives_state_machine(self, admin_client):
        task_id = admin_client.post(TASKS, _spawn_payload(), format="json").json()["id"]
        resp = admin_client.patch(f"{TASKS}{task_id}/", {"status": TaskStatus.IN_PROGRESS.value}, format="json")
        assert resp.status_code == 200
        assert resp.json()["status"] == TaskStatus.IN_PROGRESS.value

    def test_patch_illegal_transition_is_400(self, admin_client):
        task_id = admin_client.post(TASKS, _spawn_payload(), format="json").json()["id"]
        resp = admin_client.patch(f"{TASKS}{task_id}/", {"status": TaskStatus.DONE.value}, format="json")
        assert resp.status_code == 400  # open -> done is not allowed (must claim first)

    def test_retrieve_missing_task_is_404(self, admin_client):
        resp = admin_client.get(f"{TASKS}999999/")
        assert resp.status_code == 404
        assert resp.json()["error"] == "NOT_FOUND"


# --- Proposals -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestProposals:
    def test_intake_returns_201_pending(self, admin_client, fake_adapter):
        resp = admin_client.post(PROPOSALS, _intake_payload(), format="json")
        assert resp.status_code == 201
        assert resp.json()["status"] == ProposalStatus.PENDING.value

    def test_intake_rejects_oversized_proposed_value(self, admin_client, fake_adapter):
        resp = admin_client.post(PROPOSALS, _intake_payload(proposed_value={"text": "x" * 70000}), format="json")
        assert resp.status_code == 400  # > 64 KiB cap

    def test_intake_is_idempotent_on_external_ref(self, admin_client, fake_adapter):
        first = admin_client.post(PROPOSALS, _intake_payload(external_ref="n8n-1"), format="json").json()
        second = admin_client.post(
            PROPOSALS, _intake_payload(external_ref="n8n-1", proposed_value={"text": "other"}), format="json"
        ).json()
        assert first["id"] == second["id"]  # same proposal, not a duplicate

    def test_list_proposals_default_pending(self, admin_client, fake_adapter):
        admin_client.post(PROPOSALS, _intake_payload(), format="json")
        resp = admin_client.get(PROPOSALS, {"target_module": "pim"})
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    def test_list_rejects_unknown_ordering(self, admin_client):
        resp = admin_client.get(PROPOSALS, {"ordering": "value_txt_t9n"})
        assert resp.status_code == 400  # sort allowlist

    def test_accept_applies_through_adapter(self, admin_client, fake_adapter):
        pid = admin_client.post(PROPOSALS, _intake_payload(), format="json").json()["id"]
        resp = admin_client.post(f"{PROPOSALS}{pid}/accept/")
        assert resp.status_code == 200
        assert resp.json()["status"] == ProposalStatus.APPLIED.value
        assert fake_adapter.read_current(
            subject_ref="SKU-1", target_kind="attribute_value", target_locator=_LOCATOR
        ) == {"text": "Better copy."}

    def test_reject_sets_reason(self, admin_client, fake_adapter):
        pid = admin_client.post(PROPOSALS, _intake_payload(), format="json").json()["id"]
        resp = admin_client.post(f"{PROPOSALS}{pid}/reject/", {"reason": "off-brand"}, format="json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == ProposalStatus.REJECTED.value
        assert body["reject_reason"] == "off-brand"

    def test_accept_missing_proposal_is_404(self, admin_client, fake_adapter):
        resp = admin_client.post(f"{PROPOSALS}999999/accept/")
        assert resp.status_code == 404

    def test_bulk_accept_below_threshold_is_sync(self, admin_client, fake_adapter):
        admin_client.post(PROPOSALS, _intake_payload(subject_ref="SKU-A"), format="json")
        admin_client.post(PROPOSALS, _intake_payload(subject_ref="SKU-B"), format="json")
        resp = admin_client.post(f"{PROPOSALS}bulk-accept/", {"target_module": "pim"}, format="json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "sync"
        assert body["enqueued"] is None
        assert len(body["applied"]) == 2

    def test_bulk_reject_returns_count(self, admin_client, fake_adapter):
        admin_client.post(PROPOSALS, _intake_payload(subject_ref="SKU-A"), format="json")
        resp = admin_client.post(f"{PROPOSALS}bulk-reject/", {"target_module": "pim", "reason": "noise"}, format="json")
        assert resp.status_code == 200
        assert resp.json()["rejected"] == 1

    def test_bulk_undo_reverts_applied(self, admin_client, fake_adapter):
        # intake -> accept -> applied, then bulk-undo the filtered applied set.
        for sku in ("SKU-A", "SKU-B"):
            pid = admin_client.post(PROPOSALS, _intake_payload(subject_ref=sku), format="json").json()["id"]
            admin_client.post(f"{PROPOSALS}{pid}/accept/")
        resp = admin_client.post(f"{PROPOSALS}bulk-undo/", {"target_module": "pim"}, format="json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "sync"
        assert len(body["reverted"]) == 2
        assert ContentProposal.objects.filter(status=ProposalStatus.REVERTED.value).count() == 2

    def test_bulk_undo_requires_admin(self, regular_client, fake_adapter):
        resp = regular_client.post(f"{PROPOSALS}bulk-undo/", {"target_module": "pim"}, format="json")
        assert resp.status_code == 403

    def test_bulk_actions_are_throttled(self):
        # Mass-mutating bulk endpoints must carry a throttle (review-security etap-09); single-row
        # operator actions must not.
        from django_enrichment.api.admin.throttling import BulkActionThrottle, IntakeThrottle
        from django_enrichment.api.admin.views.proposal_views import ProposalViewSet

        def throttles_for(action):
            vs = ProposalViewSet()
            vs.action = action
            return [type(t) for t in vs.get_throttles()]

        assert throttles_for("bulk_undo") == [BulkActionThrottle]
        assert throttles_for("bulk_accept") == [BulkActionThrottle]
        assert throttles_for("bulk_reject") == [BulkActionThrottle]
        assert throttles_for("create") == [IntakeThrottle]
        assert throttles_for("accept") == []


# --- CSV import (etap-11) ------------------------------------------------------------------

IMPORT_CSV = f"{TASKS}import-csv/"
_CSV_BODY = (
    "sku,field,type\nENT-S001,description,fix-attribute\nENT-S002,description,fix-attribute\nENT-S001,name,translate\n"
)


def _csv_upload(text=_CSV_BODY, name="fields.csv"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


@pytest.mark.django_db
class TestCSVImport:
    def test_upload_csv_spawns_n_tasks_grouped_by_type(self, admin_client, settings, tmp_path):
        settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
        resp = admin_client.post(
            IMPORT_CSV, {"file": _csv_upload(), "channel": "default", "language": "pl"}, format="multipart"
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["task_count"] == 2
        assert body["staged_file"]
        assert {t["type"] for t in body["tasks"]} == {"fix-attribute", "translate"}
        assert all(t["scope_spec"]["mode"] == "csv" for t in body["tasks"])

    def test_upload_csv_targets_stream_from_file(self, admin_client, settings, tmp_path):
        settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
        resp = admin_client.post(
            IMPORT_CSV, {"file": _csv_upload(), "channel": "default", "language": "pl"}, format="multipart"
        )
        fix_task = next(t for t in resp.json()["tasks"] if t["type"] == "fix-attribute")

        targets = admin_client.get(f"{TASKS}{fix_task['id']}/targets/")
        assert targets.status_code == 200
        rows = targets.json()["targets"]
        assert len(rows) == 2  # the two fix-attribute rows, not the translate one
        assert {r["subject_ref"] for r in rows} == {"ENT-S001", "ENT-S002"}

    def test_upload_without_file_is_400(self, admin_client):
        resp = admin_client.post(IMPORT_CSV, {"channel": "default", "language": "pl"}, format="multipart")
        assert resp.status_code == 400

    def test_upload_bad_header_is_400_envelope(self, admin_client, settings, tmp_path):
        settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
        resp = admin_client.post(
            IMPORT_CSV,
            {"file": _csv_upload("a,b,c\n1,2,3\n"), "channel": "default", "language": "pl"},
            format="multipart",
        )
        assert resp.status_code == 400
        assert "debug_id" in resp.json()

    def test_upload_requires_auth(self, api_client):
        resp = api_client.post(IMPORT_CSV, {"channel": "default", "language": "pl"}, format="multipart")
        assert resp.status_code == 401

    def test_upload_requires_admin(self, regular_client):
        resp = regular_client.post(IMPORT_CSV, {"channel": "default", "language": "pl"}, format="multipart")
        assert resp.status_code == 403

    def test_targets_rejects_non_numeric_page(self, admin_client, settings, tmp_path):
        settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
        resp = admin_client.post(
            IMPORT_CSV, {"file": _csv_upload(), "channel": "default", "language": "pl"}, format="multipart"
        )
        task_id = resp.json()["tasks"][0]["id"]
        bad = admin_client.get(f"{TASKS}{task_id}/targets/?page=abc")
        assert bad.status_code == 400  # parsed at the boundary, not a 500

    def test_spawn_endpoints_are_throttled(self):
        # POST /tasks + import-csv carry the spawn throttle (review-security etap-11); reads do not.
        from django_enrichment.api.admin.throttling import SpawnThrottle
        from django_enrichment.api.admin.views.task_views import TaskViewSet

        def throttles_for(action):
            vs = TaskViewSet()
            vs.action = action
            return [type(t) for t in vs.get_throttles()]

        assert throttles_for("create") == [SpawnThrottle]
        assert throttles_for("import_csv") == [SpawnThrottle]
        assert throttles_for("list") == []
        assert throttles_for("targets") == []
