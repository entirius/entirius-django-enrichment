# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Product video (Amendment A1): mp4 intake, the toolbox media job calls (respx) and kind `video`."""

import base64
from types import SimpleNamespace

import httpx
import pytest
import respx
from django.core.files.uploadedfile import SimpleUploadedFile

from django_enrichment.enums import TaskStatus
from django_enrichment.kinds.video import VideoKind
from django_enrichment.models import ContentProposal
from django_enrichment.services import proposal_service, staging_store, task_service, toolbox_calls
from tests.queue_helpers import run_queue

_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
_JOB = "3f2b8c1e-0000-4000-8000-000000000000"
MEDIA = "http://toolbox.test/api/ai-media/v2/admin/shop/videos/"


@pytest.fixture
def staging_dir(settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    return tmp_path


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(toolbox_calls.time, "sleep", lambda _s: None)


@pytest.fixture
def main_picture(fake_adapter, monkeypatch):
    calls = []

    def _main_picture(*, subject_ref, channel):
        calls.append((subject_ref, channel))
        return _PNG, "image/png"

    monkeypatch.setattr(fake_adapter, "main_picture", _main_picture, raising=False)
    return calls


def _intake(content, target_kind="video", name="clip.mp4"):
    return proposal_service.intake_media(
        file=SimpleUploadedFile(name, content),
        target_module="pim",
        subject_ref="SKU-1",
        target_locator={"channel": "default"},
        proposed_value={},
        target_kind=target_kind,
    )


def _mock_job(router, *statuses, error=None):
    submit = router.post(MEDIA).mock(return_value=httpx.Response(202, json={"job_id": _JOB, "status": "pending"}))
    bodies = [httpx.Response(200, json={"status": s, "error": error}) for s in statuses]
    router.get(f"{MEDIA}{_JOB}/").mock(side_effect=bodies)
    router.get(f"{MEDIA}{_JOB}/download/").mock(return_value=httpx.Response(200, content=_MP4))
    return submit


def _generate():
    form = {"preset": "orbit", "prompt": "p", "aspect_ratio": "9:16", "duration_s": "4"}
    return toolbox_calls.generate_video(image=_PNG, mime="image/png", form=form).value


# ── intake_media: video target kind ───────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestVideoIntake:
    def test_mp4_is_staged_as_video(self, staging_dir, fake_adapter):
        proposal = _intake(_MP4)
        assert proposal.target_kind == "video"
        assert proposal.staged_file.endswith(".mp4")
        assert staging_store.exists(proposal.staged_file)

    def test_picture_kind_rejects_mp4(self, staging_dir, fake_adapter):
        with pytest.raises(ValueError, match="unsupported image type"):
            _intake(_MP4, target_kind="picture")

    def test_video_kind_rejects_png(self, staging_dir, fake_adapter):
        with pytest.raises(ValueError, match="unsupported video type"):
            _intake(_PNG, name="x.png")

    @pytest.mark.parametrize("brand", [b"heic", b"qt  ", b"M4A ", b"3gp4"])
    def test_video_kind_rejects_non_mp4_iso_bmff(self, staging_dir, fake_adapter, brand):
        with pytest.raises(ValueError, match="unsupported video type"):
            _intake(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 64)

    def test_video_cap(self, staging_dir, fake_adapter, settings):
        settings.ENRICHMENT_MAX_VIDEO_BYTES = 10
        with pytest.raises(ValueError, match="cap"):
            _intake(_MP4)
        assert list(staging_dir.iterdir()) == []

    def test_unknown_target_kind(self, staging_dir, fake_adapter):
        with pytest.raises(ValueError, match="target_kind"):
            _intake(_MP4, target_kind="attribute_value")


# ── toolbox media job API ─────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("toolbox_settings", "no_sleep")
class TestGenerateVideo:
    def test_submit_poll_download(self):
        with respx.mock() as router:
            submit = _mock_job(router, "pending", "running", "done")
            assert _generate() == _MP4
        request = submit.calls[0].request
        assert request.headers["X-API-Key"] == "test-key"
        body = request.content
        assert b'name="preset"\r\n\r\norbit' in body
        assert b'name="duration_s"\r\n\r\n4' in body
        assert b'name="image"; filename="image"' in body

    def test_failed_job_raises(self):
        with respx.mock(assert_all_called=False) as router:
            _mock_job(router, "failed", error={"error": "VIDEO_BLOCKED", "message": "blocked"})
            with pytest.raises(ValueError, match="VIDEO_BLOCKED"):
                _generate()

    def test_timeout_raises(self, settings):
        settings.ENRICHMENT_VIDEO_TIMEOUT_S = 0
        with respx.mock(assert_all_called=False) as router:
            _mock_job(router)
            with pytest.raises(TimeoutError):
                _generate()

    def test_video_endpoint_overrides(self, settings):
        settings.ENRICHMENT_VIDEO_TOOLBOX_URL = "http://media.local/"
        settings.ENRICHMENT_VIDEO_TOOLBOX_API_KEY = "media-key"
        settings.ENRICHMENT_VIDEO_TOOLBOX_CHANNEL = "local"
        url = "http://media.local/api/ai-media/v2/admin/local/videos/"
        with respx.mock() as router:
            submit = router.post(url).mock(return_value=httpx.Response(202, json={"job_id": _JOB}))
            router.get(f"{url}{_JOB}/").mock(return_value=httpx.Response(200, json={"status": "done"}))
            router.get(f"{url}{_JOB}/download/").mock(return_value=httpx.Response(200, content=_MP4))
            assert _generate() == _MP4
        assert submit.calls[0].request.headers["X-API-Key"] == "media-key"

    def test_not_configured(self, settings):
        settings.AI_TOOLBOX_API_KEY = ""
        with pytest.raises(ValueError, match="not configured"):
            _generate()


# ── kind `video` + runner ─────────────────────────────────────────────────────────────────────


def _worker(**over):
    fields = {"key": "pim.social_video", "version": 1, "target_module": "pim", "system_prompt": "Calm.", "model": ""}
    return SimpleNamespace(**{"options": {"preset": "orbit", "duration_s": 6, "aspect": "16:9"}, **fields, **over})


@pytest.mark.usefixtures("toolbox_settings", "no_sleep")
class TestVideoKind:
    def test_one_media_draft_per_subject(self, main_picture):
        task = SimpleNamespace(params={"channel": "default", "prompt": "Wooden table."})
        with respx.mock() as router:
            submit = _mock_job(router, "done")
            [draft] = VideoKind().run(_worker(), task, "SKU-1")
        assert main_picture == [("SKU-1", "default")]
        assert (draft.target_kind, draft.target_locator) == ("video", {"channel": "default"})
        assert draft.media_file.read() == _MP4
        body = submit.calls[0].request.content
        assert b"Calm.\n\nWooden table." in body
        assert b'name="aspect_ratio"\r\n\r\n16:9' in body

    def test_adapter_without_main_picture_fails(self, fake_adapter):
        task = SimpleNamespace(params={"channel": "default"})
        with pytest.raises(ValueError, match="main_picture"):
            VideoKind().run(_worker(), task, "SKU-1")

    @pytest.mark.django_db
    def test_runner_stages_video_proposal(self, main_picture, make_worker, staging_dir):
        make_worker(key="pim.social_video", kind="video", options={"preset": "orbit"}, system_prompt="Calm.")
        scope = {"mode": "list", "module": "pim", "refs": ["SKU-1"]}
        task = task_service.spawn(type="pim.social_video", scope_spec=scope, params={"channel": "default"})
        with respx.mock() as router:
            _mock_job(router, "done")
            run_queue(task)
        task.refresh_from_db()
        proposal = ContentProposal.objects.get(task=task)
        assert task.status == TaskStatus.DONE.value
        assert (proposal.target_kind, proposal.source) == ("video", "worker:pim.social_video@v1")
        assert staging_store.exists(proposal.staged_file)
