# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Image processing (plan 25): the toolbox image job (respx), kind `image`, intake of N outputs and the
accept mode (`add` | `replace`) — single accept, bulk accept (always add) and undo."""

import base64
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx
from django_utils.toolbox import ToolboxError

from django_enrichment.enums import ProposalStatus, TaskStatus
from django_enrichment.kinds.base import SubjectError
from django_enrichment.kinds.image import ImageKind
from django_enrichment.models import ContentProposal
from django_enrichment.services import proposal_service, task_service, template_service, toolbox_calls, undo_service
from tests.queue_helpers import run_queue

_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
_OUT = [_PNG + b"\x00", _PNG + b"\x00\x00"]
_JOB = "7a1c0d2e-0000-4000-8000-000000000000"
IMAGES = "http://toolbox.test/api/ai-media/v2/admin/shop/images/"
PROPOSALS = "/api/enrichment/v2/admin/proposals/"
_PICTURES = [{"ref": "3", "label": "Side", "url": "/s.png", "is_main": False}, {"ref": "5", "is_main": True}]


@pytest.fixture
def staging_dir(settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    return tmp_path


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(toolbox_calls.time, "sleep", lambda _s: None)


@pytest.fixture
def pictures(fake_adapter, monkeypatch):
    """`list_pictures` + `picture` on the fake adapter; records the refs read. Ref "99" is not the subject's."""
    reads = []

    def _picture(*, subject_ref, channel, picture_ref):
        if picture_ref == "99":
            raise LookupError(picture_ref)
        reads.append(picture_ref)
        return _PNG, "image/png"

    monkeypatch.setattr(fake_adapter, "list_pictures", lambda **_kw: list(_PICTURES), raising=False)
    monkeypatch.setattr(fake_adapter, "picture", _picture, raising=False)
    return reads


@pytest.fixture
def modes(fake_adapter, monkeypatch):
    """The fake adapter offers add/replace for a locator with a source picture and records each apply's mode."""
    applied_modes = []
    original_apply = fake_adapter.apply

    def _apply_modes(*, target_kind, target_locator):
        return ["add", "replace"] if target_locator.get("source_picture_ref") else []

    def _apply(proposal):
        applied_modes.append(proposal.apply_mode)
        return original_apply(proposal)

    monkeypatch.setattr(fake_adapter, "apply_modes", _apply_modes, raising=False)
    monkeypatch.setattr(fake_adapter, "apply", _apply)
    return applied_modes


def _mock_job(router, *statuses, outputs=2, cost="0.078", error=None):
    submit = router.post(IMAGES).mock(return_value=httpx.Response(202, json={"job_id": _JOB, "status": "pending"}))
    listed = [{"index": k, "mime_type": "image/png"} for k in range(outputs)]
    bodies = [
        httpx.Response(200, json={"status": s, "outputs": listed, "cost": cost, "error": error}) for s in statuses
    ]
    router.get(f"{IMAGES}{_JOB}/").mock(side_effect=bodies)
    for k in range(outputs):
        router.get(f"{IMAGES}{_JOB}/outputs/{k}/download/").mock(return_value=httpx.Response(200, content=_OUT[k]))
    return submit


def _generate(task=None):
    form = {"operation": "packshot", "prompt": "", "n": "2"}
    return toolbox_calls.generate_image(image=_PNG, mime="image/png", form=form, task=task)


def _worker(**over):
    fields = {"key": "pim.image", "version": 1, "target_module": "pim", "system_prompt": "Keep the label.", "model": ""}
    return SimpleNamespace(**{"options": {}, **fields, **over})


def _intake_derived(*, variant=1, locator_extra=None):
    locator = {"channel": "default", "source_picture_ref": "5", "operation": "packshot", "variant": variant}
    from django.core.files.uploadedfile import SimpleUploadedFile

    return proposal_service.intake_media(
        file=SimpleUploadedFile("out.png", _PNG),
        target_module="pim",
        subject_ref="SKU-1",
        target_locator={**locator, **(locator_extra or {})},
        proposed_value={"op": "derived", "operation": "packshot", "variant": variant, "of": 2},
    )


# ── toolbox image job API ─────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("toolbox_settings", "no_sleep")
class TestGenerateImage:
    def test_submit_poll_download_every_output(self):
        with respx.mock() as router:
            submit = _mock_job(router, "running", "done")
            result = _generate()
        assert result.value == _OUT
        assert result.cost == Decimal("0.078")
        body = submit.calls[0].request.content
        assert b'name="operation"\r\n\r\npackshot' in body
        assert b'name="n"\r\n\r\n2' in body
        assert b'name="image"; filename="image"' in body

    def test_failed_job_raises_its_code(self):
        with respx.mock(assert_all_called=False) as router:
            _mock_job(router, "failed", error={"error": "IMAGE_BLOCKED", "message": "blocked"})
            with pytest.raises(ValueError, match="IMAGE_BLOCKED"):
                _generate()

    def test_a_refused_submit_carries_the_toolbox_reason(self):
        with respx.mock() as router:
            router.post(IMAGES).mock(
                return_value=httpx.Response(402, json={"error": "BUDGET_EXCEEDED", "message": "Budget reached."})
            )
            with pytest.raises(ToolboxError) as caught:
                _generate()
        assert toolbox_calls.failure_code(caught.value) == "BUDGET_EXCEEDED"

    def test_timeout_raises(self, settings):
        settings.ENRICHMENT_IMAGE_TIMEOUT_S = 0
        with respx.mock(assert_all_called=False) as router:
            _mock_job(router)
            with pytest.raises(TimeoutError):
                _generate()

    def test_a_job_without_outputs_raises(self):
        with respx.mock() as router:
            _mock_job(router, "done", outputs=0)
            with pytest.raises(ValueError, match="without outputs"):
                _generate()


# ── kind `image` ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("toolbox_settings", "no_sleep")
class TestImageKind:
    def test_the_main_picture_by_default_one_draft_per_output(self, pictures):
        task = SimpleNamespace(params={"channel": "default", "operation": "packshot", "n": 2})
        with respx.mock() as router:
            submit = _mock_job(router, "done")
            drafts = ImageKind().run(_worker(), task, "SKU-1")
        assert pictures == ["5"]
        assert [d.target_kind for d in drafts] == ["picture", "picture"]
        assert [d.target_locator["variant"] for d in drafts] == [1, 2]
        assert drafts[0].target_locator["source_picture_ref"] == "5"
        assert drafts[1].proposed_value == {
            "op": "derived",
            "operation": "packshot",
            "preset": "",
            "variant": 2,
            "of": 2,
        }
        assert drafts[0].media_file.read() == _OUT[0]
        assert [d.cost_usd for d in drafts] == [Decimal("0.039"), Decimal("0.039")]
        assert b'name="preset"' not in submit.calls[0].request.content

    def test_a_chosen_picture_preset_and_prompt(self, pictures):
        params = {"channel": "default", "operation": "lifestyle", "preset": "lifestyle-scene", "picture_ref": "3"}
        task = SimpleNamespace(params={**params, "prompt": "Kitchen table."})
        with respx.mock() as router:
            submit = _mock_job(router, "done", outputs=1)
            [draft] = ImageKind().run(_worker(), task, "SKU-1")
        assert pictures == ["3"]
        assert draft.target_locator == {
            "channel": "default",
            "source_picture_ref": "3",
            "operation": "lifestyle",
            "variant": 1,
        }
        body = submit.calls[0].request.content
        assert b'name="preset"\r\n\r\nlifestyle-scene' in body
        assert b"Keep the label.\n\nKitchen table." in body

    def test_a_foreign_picture_is_refused(self, pictures):
        task = SimpleNamespace(params={"channel": "default", "operation": "enhance", "picture_ref": "99"})
        with pytest.raises(SubjectError) as caught:
            ImageKind().run(_worker(), task, "SKU-1")
        assert caught.value.code == "PICTURE_NOT_FOUND"

    def test_no_main_picture_fails_the_subject(self, fake_adapter, monkeypatch):
        monkeypatch.setattr(fake_adapter, "list_pictures", lambda **_kw: [], raising=False)
        task = SimpleNamespace(params={"channel": "default", "operation": "enhance"})
        with pytest.raises(SubjectError) as caught:
            ImageKind().run(_worker(), task, "SKU-1")
        assert caught.value.code == "NO_PICTURE"

    def test_an_adapter_without_pictures_fails(self, fake_adapter):
        task = SimpleNamespace(params={"channel": "default", "operation": "enhance"})
        with pytest.raises(ValueError, match="list_pictures"):
            ImageKind().run(_worker(), task, "SKU-1")

    def test_a_run_of_a_custom_instruction_needs_a_prompt(self):
        with pytest.raises(ValueError, match="needs a prompt"):
            ImageKind.Params.model_validate({"channel": "default", "operation": "instruct", "prompt": " "})

    @pytest.mark.django_db
    def test_a_custom_instruction_template_leaves_the_prompt_to_the_run(self, make_worker):
        make_worker(key="pim.image", kind="image", options={}, system_prompt="")
        template = template_service.create_template(
            key="image-custom", worker_key="pim.image", label="Custom", params={"operation": "instruct"}
        )
        assert template.params == {"operation": "instruct"}

    @pytest.mark.parametrize("n", [0, 5])
    def test_variants_are_one_to_four(self, n):
        with pytest.raises(ValueError):
            ImageKind.Params.model_validate({"channel": "default", "operation": "packshot", "n": n})

    @pytest.mark.django_db
    def test_runner_stages_one_proposal_per_output(self, pictures, make_worker, staging_dir):
        make_worker(key="pim.image", kind="image", options={}, system_prompt="")
        scope = {"mode": "list", "module": "pim", "refs": ["SKU-1"]}
        params = {"channel": "default", "operation": "packshot", "n": 2}
        task = task_service.spawn(type="pim.image", scope_spec=scope, params=params)
        with respx.mock() as router:
            _mock_job(router, "done")
            run_queue(task)
        task.refresh_from_db()
        proposals = ContentProposal.objects.filter(task=task).order_by("pk")
        assert task.status == TaskStatus.DONE.value
        assert [p.status for p in proposals] == ["pending", "pending"]  # two variants, neither supersedes the other
        assert {p.staged_file.endswith(".png") for p in proposals} == {True}


# ── accept mode ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAcceptMode:
    def test_the_proposal_offers_its_modes(self, admin_client, modes, staging_dir):
        proposal = _intake_derived()
        body = admin_client.get(f"{PROPOSALS}{proposal.pk}/").json()
        assert body["apply_modes"] == ["add", "replace"]
        assert body["apply_mode"] == ""

    def test_accept_without_a_mode_adds(self, admin_client, modes, staging_dir):
        proposal = _intake_derived()
        resp = admin_client.post(f"{PROPOSALS}{proposal.pk}/accept/")
        assert resp.status_code == 200
        assert resp.json()["apply_mode"] == "add"
        assert modes == ["add"]

    def test_accept_replace(self, admin_client, modes, staging_dir):
        proposal = _intake_derived()
        resp = admin_client.post(f"{PROPOSALS}{proposal.pk}/accept/", {"mode": "replace"}, format="json")
        assert resp.status_code == 200
        assert resp.json()["status"] == ProposalStatus.APPLIED.value
        assert modes == ["replace"]
        proposal.refresh_from_db()
        assert proposal.apply_mode == "replace"

    def test_replace_is_refused_where_it_is_not_offered(self, admin_client, modes, staging_dir):
        proposal = _intake_derived(locator_extra={"source_picture_ref": ""})
        resp = admin_client.post(f"{PROPOSALS}{proposal.pk}/accept/", {"mode": "replace"}, format="json")
        assert resp.status_code == 400
        proposal.refresh_from_db()
        assert proposal.status == ProposalStatus.PENDING.value
        assert modes == []

    def test_an_unknown_mode_is_a_validation_error(self, admin_client, modes, staging_dir):
        proposal = _intake_derived()
        resp = admin_client.post(f"{PROPOSALS}{proposal.pk}/accept/", {"mode": "merge"}, format="json")
        assert resp.status_code == 400

    def test_no_choice_keeps_the_mode_blank(self, admin_client, fake_adapter):
        payload = {
            "target_module": "pim",
            "subject_ref": "SKU-1",
            "target_kind": "attribute_value",
            "target_locator": {"channel": "default", "language": "pl", "feature_idx": "description"},
            "proposed_value": {"text": "Copy."},
        }
        pid = admin_client.post(PROPOSALS, payload, format="json").json()["id"]
        body = admin_client.post(f"{PROPOSALS}{pid}/accept/", {"mode": "add"}, format="json").json()
        assert (body["status"], body["apply_mode"], body["apply_modes"]) == ("applied", "", [])

    def test_bulk_accept_always_adds(self, admin_client, modes, staging_dir):
        _intake_derived(variant=1)
        _intake_derived(variant=2)
        resp = admin_client.post(f"{PROPOSALS}bulk-accept/", {"target_module": "pim"}, format="json")
        assert len(resp.json()["applied"]) == 2
        assert modes == ["add", "add"]

    def test_undo_sees_the_mode_it_was_applied_with(self, modes, staging_dir, monkeypatch, fake_adapter):
        reverted_modes = []
        original_revert = fake_adapter.revert
        monkeypatch.setattr(
            fake_adapter, "revert", lambda p: (reverted_modes.append(p.apply_mode), original_revert(p))[1]
        )
        proposal = proposal_service.accept(_intake_derived(), mode="replace")

        undo_service.revert(proposal)

        assert reverted_modes == ["replace"]
        assert proposal.status == ProposalStatus.REVERTED.value
