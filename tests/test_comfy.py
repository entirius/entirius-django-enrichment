# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `comfy`: the ComfyUI client against a fake server (respx), the kind's verdicts and drafts through the real
work queue, `auto_apply`, stored results and their re-judging, and the spawn (`enrichment.comfy_spawn`)."""

import base64
import hashlib
import json

import httpx
import pytest
import respx
from django.core.cache import cache
from django.core.files.base import ContentFile

from django_enrichment.enums import ProposalStatus, TaskStatus
from django_enrichment.kinds import comfy
from django_enrichment.kinds.base import ProposalDraft, SubjectError
from django_enrichment.models import ComfyResult, ContentProposal, EnrichmentTask
from django_enrichment.services import (
    comfy_spawn_service,
    comfyui_client,
    task_service,
    toolbox_calls,
    worker_service,
)
from django_enrichment.tasks import comfy_spawn_task
from tests.queue_helpers import run_queue

COMFY = "http://comfy.test"
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
_OUT = _PNG + b"\x01"
_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "INPUT.png"}},
    "2": {"class_type": "BiRefNetRMBG", "inputs": {"image": ["1", 0]}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "out"}},
}
_FLAG = {"channel": "default", "language": None, "feature_idx": "light_packshot"}
_CHECK = {"channel": "default", "language": None, "feature_idx": "packshot_check"}
_THRESHOLDS = [{"min": 0.3, "action": "set"}, {"min": 0.15, "action": "review"}]


# ── fixtures ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def comfy_settings(settings, tmp_path):
    settings.ENRICHMENT_COMFYUI_URL = COMFY
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path / "staging")
    settings.BASE_DIR = tmp_path
    (tmp_path / "workflow.json").write_text(json.dumps(_WORKFLOW))
    cache.clear()
    return settings


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(comfyui_client.time, "sleep", lambda _s: None)


@pytest.fixture
def scores(monkeypatch):
    """Registers metric `contour` (the score each SKU's picture gets, by the bytes) and prefilter `format`."""
    by_image: dict[bytes, float] = {}
    gate: dict[bytes, comfy.Prefiltered] = {}
    monkeypatch.setattr(comfy, "_METRICS", {})
    monkeypatch.setattr(comfy, "_PREFILTERS", {})
    comfy.register_comfy_metric("contour", lambda image, output: {"score": by_image[image], "out": len(output)})
    comfy.register_comfy_prefilter("format", gate.get)
    return by_image, gate


@pytest.fixture
def catalogue(fake_adapter, monkeypatch):
    """Products with one main picture each: `catalogue["SKU"] = bytes`. `main_pictures` lists them with their sha1."""
    files: dict[str, bytes] = {}

    def _main_pictures(*, channel, page=1):
        rows = [
            {"subject_ref": sku, "picture_ref": f"p-{sku}", "sha1": _sha1(data), "width": 1, "height": 1}
            for sku, data in files.items()
        ]
        return rows[(page - 1) * 2 : page * 2]  # pages of two

    def _picture(*, subject_ref, channel, picture_ref):
        return files[subject_ref], "image/png"

    monkeypatch.setattr(
        fake_adapter,
        "list_pictures",
        lambda *, subject_ref, channel: [{"ref": f"p-{subject_ref}", "is_main": True}],
        raising=False,
    )
    monkeypatch.setattr(fake_adapter, "picture", _picture, raising=False)
    monkeypatch.setattr(fake_adapter, "main_pictures", _main_pictures, raising=False)
    return files


@pytest.fixture
def make_comfy(make_worker, comfy_settings, scores):
    def _make(key="pim.packshot", **options):
        base = {
            "channel": "default",
            "workflow": "workflow.json",
            "input_node": "1",
            "metric": "contour",
            "thresholds": _THRESHOLDS,
            "feature": "light_packshot",
        }
        return make_worker(key=key, label="Packshot", kind="comfy", options={**base, **options})

    return _make


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def _picture(n: int) -> bytes:
    return _PNG + bytes([n]) * (n + 2)


def _mock_comfy(router, *, status="success", outputs=None, history_polls=1):
    """A ComfyUI that stores the upload, queues the prompt and finishes it after `history_polls` empty polls."""
    upload = router.post(f"{COMFY}/upload/image").mock(
        return_value=httpx.Response(200, json={"name": "in.png", "subfolder": "", "type": "input"})
    )
    prompt = router.post(f"{COMFY}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "abc"}))
    images = (
        outputs
        if outputs is not None
        else {"3": {"images": [{"filename": "out_1.png", "subfolder": "", "type": "output"}]}}
    )
    entry = {"abc": {"status": {"status_str": status}, "outputs": images}}
    router.get(f"{COMFY}/history/abc").mock(
        side_effect=[httpx.Response(200, json={})] * history_polls + [httpx.Response(200, json=entry)] * 50
    )
    view = router.get(f"{COMFY}/view").mock(return_value=httpx.Response(200, content=_OUT))
    return upload, prompt, view


def _run(worker, *refs):
    scope = {"mode": "list", "module": "pim", "refs": list(refs)}
    return run_queue(task_service.spawn(type=worker.key, scope_spec=scope, params={}, lane="queue"))


def _proposals(**filters):
    return list(ContentProposal.objects.filter(**filters).order_by("pk"))


# ── ComfyUI client ──────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("comfy_settings")
class TestClient:
    def test_upload_prompt_poll_view(self):
        with respx.mock() as router:
            upload, prompt, view = _mock_comfy(router, history_polls=2)
            output = comfyui_client.run(_WORKFLOW, input_node="1", image=_PNG, filename="x.png")
        assert output == _OUT
        assert b'filename="x.png"' in upload.calls[0].request.content
        sent = json.loads(prompt.calls[0].request.content)["prompt"]
        assert sent["1"]["inputs"]["image"] == "in.png"
        assert sent["3"] == _WORKFLOW["3"]
        assert _WORKFLOW["1"]["inputs"]["image"] == "INPUT.png"  # the caller's workflow is not changed
        assert dict(view.calls[0].request.url.params) == {"filename": "out_1.png", "subfolder": "", "type": "output"}

    def test_an_upload_in_a_subfolder_is_named_with_it(self):
        with respx.mock() as router:
            _, prompt, _ = _mock_comfy(router)
            router.post(f"{COMFY}/upload/image").mock(
                return_value=httpx.Response(200, json={"name": "in.png", "subfolder": "enr", "type": "input"})
            )
            comfyui_client.run(_WORKFLOW, input_node="1", image=_PNG, filename="x.png")
        assert json.loads(prompt.calls[0].request.content)["prompt"]["1"]["inputs"]["image"] == "enr/in.png"

    @pytest.mark.parametrize(
        ("mock", "code"),
        [
            ({"status": "error"}, "COMFYUI_FAILED"),
            ({"outputs": {"3": {"images": []}}}, "COMFYUI_NO_OUTPUT"),
        ],
    )
    def test_a_failed_or_empty_prompt_raises_its_code(self, mock, code):
        with respx.mock(assert_all_called=False) as router:
            _mock_comfy(router, **mock)
            with pytest.raises(SubjectError) as caught:
                comfyui_client.run(_WORKFLOW, input_node="1", image=_PNG, filename="x.png")
        assert caught.value.code == code

    def test_a_refused_prompt_carries_the_status(self):
        with respx.mock(assert_all_called=False) as router:
            _mock_comfy(router)
            router.post(f"{COMFY}/prompt").mock(return_value=httpx.Response(400, json={"error": "bad node"}))
            with pytest.raises(SubjectError) as caught:
                comfyui_client.run(_WORKFLOW, input_node="1", image=_PNG, filename="x.png")
        assert caught.value.code == "COMFYUI_HTTP_400"

    @pytest.mark.parametrize(
        ("raised", "code"),
        [(httpx.ConnectError("refused"), "COMFYUI_UNAVAILABLE"), (httpx.ReadTimeout("slow"), "TIMEOUT")],
    )
    def test_a_transport_error_has_a_stable_code(self, raised, code):
        with respx.mock() as router:
            router.post(f"{COMFY}/upload/image").mock(side_effect=raised)
            with pytest.raises((SubjectError, TimeoutError)) as caught:
                comfyui_client.run(_WORKFLOW, input_node="1", image=_PNG, filename="x.png")
        assert toolbox_calls.failure_code(caught.value) == code

    def test_a_prompt_that_never_finishes_times_out(self, settings):
        settings.ENRICHMENT_COMFYUI_TIMEOUT_S = 0
        with respx.mock(assert_all_called=False) as router:
            _mock_comfy(router)
            with pytest.raises(TimeoutError):
                comfyui_client.run(_WORKFLOW, input_node="1", image=_PNG, filename="x.png")

    def test_not_configured(self, settings):
        settings.ENRICHMENT_COMFYUI_URL = ""
        with pytest.raises(SubjectError) as caught:
            comfyui_client.run(_WORKFLOW, input_node="1", image=_PNG, filename="x.png")
        assert caught.value.code == "COMFYUI_NOT_CONFIGURED"
        assert comfyui_client.available() is False

    def test_available(self):
        with respx.mock() as router:
            router.get(f"{COMFY}/system_stats").mock(return_value=httpx.Response(200, json={}))
            assert comfyui_client.available() is True
        with respx.mock() as router:
            router.get(f"{COMFY}/system_stats").mock(side_effect=httpx.ConnectError("refused"))
            assert comfyui_client.available() is False


# ── options ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("scores")
class TestOptions:
    _BASE = {"channel": "default", "workflow": "w.json", "input_node": "1", "metric": "contour"}

    @pytest.mark.parametrize(
        ("over", "message"),
        [
            ({"metric": "nope"}, "unknown metric"),
            ({"prefilter": "nope"}, "unknown prefilter"),
            ({"thresholds": [{"min": 0.3, "action": "set"}]}, "need `feature`"),
            ({"thresholds": [{"min": 0.3, "action": "escalate", "reason": "x"}]}, "needs `escalate_feature`"),
            ({"thresholds": [{"min": 0.3, "action": "escalate"}], "escalate_feature": "c"}, "needs a `reason`"),
            (
                {"output": "picture", "metric": "", "thresholds": [{"min": 0.3, "action": "review"}], "feature": "f"},
                "no thresholds",
            ),
        ],
    )
    def test_inconsistent_options_are_refused(self, over, message):
        with pytest.raises(ValueError, match=message):
            comfy.ComfyOptions.model_validate({**self._BASE, **over})

    def test_a_picture_workflow_needs_no_metric(self):
        comfy.ComfyOptions.model_validate({**self._BASE, "output": "picture", "metric": ""})

    def test_auto_apply_never_takes_a_media_draft(self):
        with pytest.raises(ValueError, match="never applied automatically"):
            ProposalDraft("picture", {}, {}, media_file=ContentFile(_PNG, name="x.png"), auto_apply=True)


# ── kind through the work queue ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestVerdicts:
    def test_set_is_applied_at_once_review_waits(self, make_comfy, catalogue, scores, fake_adapter):
        by_image, _ = scores
        catalogue.update({"LIGHT": _picture(1), "EDGE": _picture(2), "DARK": _picture(3)})
        by_image.update({_picture(1): 0.42, _picture(2): 0.2, _picture(3): 0.01})
        worker = make_comfy()
        with respx.mock() as router:
            _, prompt, _ = _mock_comfy(router)
            task = _run(worker, "LIGHT", "EDGE", "DARK")

        assert task.status == TaskStatus.DONE.value
        assert prompt.call_count == 3
        light, edge = _proposals()
        assert (light.subject_ref, light.status, light.proposed_value) == ("LIGHT", "applied", {"value": True})
        assert fake_adapter.read_current(subject_ref="LIGHT", target_kind="attribute_value", target_locator=_FLAG) == {
            "value": True
        }
        assert (edge.subject_ref, edge.status, edge.proposed_value) == ("EDGE", "pending", {"value": True})
        assert light.source == f"worker:pim.packshot@v{worker.version}"
        verdicts = dict(ComfyResult.objects.values_list("subject_ref", "verdict"))
        assert verdicts == {"LIGHT": "set", "EDGE": "review", "DARK": "pass"}
        assert ComfyResult.objects.get(subject_ref="LIGHT").metrics == {"score": 0.42, "out": len(_OUT)}

    def test_pass_proposes_false_only_over_a_true_flag(self, make_comfy, catalogue, scores, fake_adapter):
        by_image, _ = scores
        catalogue.update({"WAS": _picture(1), "NEVER": _picture(2)})
        by_image.update({_picture(1): 0.01, _picture(2): 0.01})
        fake_adapter.set_current("WAS", "attribute_value", _FLAG, {"value": True})
        with respx.mock() as router:
            _mock_comfy(router)
            _run(make_comfy(), "WAS", "NEVER")
        [proposal] = _proposals()
        assert (proposal.subject_ref, proposal.status, proposal.proposed_value) == ("WAS", "pending", {"value": False})

    def test_a_flag_already_true_gets_no_proposal(self, make_comfy, catalogue, scores, fake_adapter):
        by_image, _ = scores
        catalogue["LIGHT"] = _picture(1)
        by_image[_picture(1)] = 0.9
        fake_adapter.set_current("LIGHT", "attribute_value", _FLAG, {"value": True})
        with respx.mock() as router:
            _mock_comfy(router)
            _run(make_comfy(), "LIGHT")
        assert _proposals() == []
        assert ComfyResult.objects.get().verdict == "set"

    def test_a_failing_auto_apply_leaves_the_proposal_pending(
        self, make_comfy, catalogue, scores, fake_adapter, monkeypatch
    ):
        by_image, _ = scores
        catalogue["LIGHT"] = _picture(1)
        by_image[_picture(1)] = 0.9

        def _refuse(proposal):
            raise ValueError("feature not in the feature set")

        monkeypatch.setattr(fake_adapter, "apply", _refuse)
        with respx.mock() as router:
            _mock_comfy(router)
            task = _run(make_comfy(), "LIGHT")
        [proposal] = _proposals()
        assert proposal.status == ProposalStatus.PENDING.value
        assert task.counts.get("failed", 0) == 0

    def test_dry_run_stores_results_only(self, make_comfy, catalogue, scores):
        by_image, _ = scores
        catalogue["LIGHT"] = _picture(1)
        by_image[_picture(1)] = 0.9
        with respx.mock() as router:
            _mock_comfy(router)
            _run(make_comfy(dry_run=True), "LIGHT")
        assert _proposals() == []
        assert ComfyResult.objects.get().verdict == "set"


@pytest.mark.django_db
class TestPrefilterAndEscalation:
    def test_a_prefilter_verdict_needs_no_gpu(self, make_comfy, catalogue, scores):
        _, gate = scores
        catalogue["PLAIN"] = _picture(1)
        gate[_picture(1)] = comfy.Prefiltered("pass", metrics={"off": 0.01})
        with respx.mock(assert_all_called=False) as router:
            _, prompt, _ = _mock_comfy(router)
            _run(make_comfy(prefilter="format"), "PLAIN")
        assert prompt.call_count == 0
        result = ComfyResult.objects.get()
        assert (result.source, result.verdict, result.metrics) == ("prefilter", "pass", {"off": 0.01})

    def test_escalation_is_applied_and_put_back_for_a_new_file(self, make_comfy, catalogue, scores, fake_adapter):
        by_image, gate = scores
        catalogue["SMALL"] = _picture(1)
        gate[_picture(1)] = comfy.Prefiltered("escalate", reason="replace_size")
        worker = make_comfy(prefilter="format", escalate_feature="packshot_check", escalate_ok="ok")
        with respx.mock(assert_all_called=False) as router:
            _mock_comfy(router)
            _run(worker, "SMALL")
            assert fake_adapter.read_current(
                subject_ref="SMALL", target_kind="attribute_value", target_locator=_CHECK
            ) == {"option": "replace_size"}
            catalogue["SMALL"] = _picture(2)  # the client sent a proper packshot
            by_image[_picture(2)] = 0.01
            _run(worker, "SMALL")
        statuses = [(p.target_locator["feature_idx"], p.proposed_value, p.status) for p in _proposals()]
        assert statuses == [
            ("packshot_check", {"option": "replace_size"}, "applied"),
            ("packshot_check", {"option": "ok"}, "applied"),
        ]


@pytest.mark.django_db
class TestFailuresAndCache:
    def test_comfyui_down_leaves_no_result_and_no_proposal(self, make_comfy, catalogue, scores):
        by_image, _ = scores
        catalogue["LIGHT"] = _picture(1)
        by_image[_picture(1)] = 0.9
        with respx.mock() as router:
            router.post(f"{COMFY}/upload/image").mock(side_effect=httpx.ConnectError("refused"))
            task = _run(make_comfy(), "LIGHT")
        assert ComfyResult.objects.count() == 0
        assert _proposals() == []
        assert task.counts["failed"] == 1
        assert task.errors[0]["code"] == "COMFYUI_UNAVAILABLE"

    def test_a_stored_result_is_rejudged_without_comfyui(self, make_comfy, catalogue, scores):
        by_image, _ = scores
        catalogue["EDGE"] = _picture(1)
        by_image[_picture(1)] = 0.2
        worker = make_comfy()
        with respx.mock() as router:
            _mock_comfy(router)
            _run(worker, "EDGE")
        worker.options = {**worker.options, "thresholds": [{"min": 0.1, "action": "set"}]}
        worker = worker_service.save_worker(worker)
        with respx.mock(assert_all_called=False) as router:
            _, prompt, _ = _mock_comfy(router)
            _run(worker, "EDGE")
        assert prompt.call_count == 0
        assert [(p.status, p.proposed_value) for p in _proposals()] == [
            ("superseded", {"value": True}),
            ("applied", {"value": True}),
        ]
        result = ComfyResult.objects.get()
        assert (result.verdict, result.worker_version) == ("set", worker.version)

    def test_the_same_file_on_two_products_runs_once_and_flags_both(self, make_comfy, catalogue, scores):
        by_image, _ = scores
        catalogue.update({"GIFT-50": _picture(1), "GIFT-100": _picture(1)})
        by_image[_picture(1)] = 0.9
        worker = make_comfy()
        with respx.mock() as router:
            _, prompt, _ = _mock_comfy(router)
            _run(worker, "GIFT-50")
            assert comfy_spawn_service.unchecked_refs(worker, limit=10) == ["GIFT-100"]
            _run(worker, "GIFT-100")
        assert prompt.call_count == 1
        assert [(p.subject_ref, p.status) for p in _proposals()] == [("GIFT-50", "applied"), ("GIFT-100", "applied")]
        assert comfy_spawn_service.unchecked_refs(worker, limit=10) == []

    def test_an_edited_workflow_runs_again(self, make_comfy, catalogue, scores, comfy_settings):
        by_image, _ = scores
        catalogue["EDGE"] = _picture(1)
        by_image[_picture(1)] = 0.01
        worker = make_comfy()
        with respx.mock() as router:
            _mock_comfy(router)
            _run(worker, "EDGE")
            (comfy_settings.BASE_DIR / "workflow.json").write_text(json.dumps({**_WORKFLOW, "4": {}}))
            _run(worker, "EDGE")
        assert ComfyResult.objects.count() == 2

    def test_a_missing_workflow_fails_the_subject(self, make_comfy, catalogue, comfy_settings):
        catalogue["X"] = _picture(1)
        worker = make_comfy()
        (comfy_settings.BASE_DIR / "workflow.json").unlink()
        task = _run(worker, "X")
        assert task.errors[0]["code"] == "COMFY_WORKFLOW_MISSING"


@pytest.mark.django_db
class TestPictureOutput:
    def test_the_output_is_a_pending_derived_picture(self, make_comfy, catalogue, scores):
        catalogue["SMALL"] = _picture(1)
        worker = make_comfy(output="picture", metric="", thresholds=[], feature="")
        with respx.mock() as router:
            _mock_comfy(router)
            _run(worker, "SMALL")
            _run(worker, "SMALL")  # the same file again: proposed once
        [proposal] = _proposals()
        assert proposal.status == ProposalStatus.PENDING.value
        assert proposal.target_kind == "picture"
        assert proposal.target_locator["source_picture_ref"] == "p-SMALL"
        assert proposal.proposed_value["op"] == "derived"
        assert proposal.open_staged_file().read() == _OUT
        assert ComfyResult.objects.get().verdict == "propose_picture"


# ── spawn ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestSpawn:
    def test_spawns_the_unchecked_pictures_on_the_bulk_lane(self, make_comfy, catalogue, scores):
        by_image, _ = scores
        for n, sku in enumerate(["A", "B", "C"], start=1):
            catalogue[sku] = _picture(n)
            by_image[_picture(n)] = 0.01
        worker = make_comfy()
        with respx.mock(assert_all_called=False) as router:
            _mock_comfy(router)
            _run(worker, "B")  # B is checked already
            router.get(f"{COMFY}/system_stats").mock(return_value=httpx.Response(200, json={}))
            task = comfy_spawn_service.spawn(worker)
        assert task.scope_spec == {"mode": "list", "module": "pim", "refs": ["A", "C"]}
        assert (task.lane, task.type, task.params) == ("queue", "pim.packshot", {})

    def test_nothing_while_a_task_runs_or_comfyui_is_down(self, make_comfy, catalogue):
        catalogue["A"] = _picture(1)
        worker = make_comfy()
        with respx.mock() as router:
            router.get(f"{COMFY}/system_stats").mock(side_effect=httpx.ConnectError("refused"))
            assert comfy_spawn_service.spawn(worker) is None
        EnrichmentTask.objects.create(type=worker.key, status=TaskStatus.IN_PROGRESS.value)
        assert comfy_spawn_service.spawn(worker) is None

    def test_a_version_bump_makes_every_picture_due(self, make_comfy, catalogue, scores):
        by_image, _ = scores
        catalogue["A"] = _picture(1)
        by_image[_picture(1)] = 0.01
        worker = make_comfy()
        with respx.mock() as router:
            _mock_comfy(router)
            _run(worker, "A")
        assert comfy_spawn_service.unchecked_refs(worker, limit=10) == []
        worker.options = {**worker.options, "dry_run": True}
        worker = worker_service.save_worker(worker)
        assert comfy_spawn_service.unchecked_refs(worker, limit=10) == ["A"]

    def test_the_cursor_walks_on_and_wraps(self, make_comfy, catalogue, fake_adapter, monkeypatch):
        for n, sku in enumerate(["A", "B", "C", "D", "E"], start=1):
            catalogue[sku] = _picture(n)
        rows = fake_adapter.main_pictures
        monkeypatch.setattr(
            fake_adapter,
            "main_pictures",
            lambda **kw: [{**r, "sha1": r["sha1"] if r["subject_ref"] != "E" else ""} for r in rows(**kw)],
        )
        worker = make_comfy()
        assert comfy_spawn_service.unchecked_refs(worker, limit=2) == ["A", "B"]
        assert comfy_spawn_service.unchecked_refs(worker, limit=2) == ["C", "D"]  # E has no sha1: left out
        assert comfy_spawn_service.unchecked_refs(worker, limit=3) == ["A", "B", "C"]

    def test_the_beat_spawns_every_worker_and_survives_a_broken_one(self, make_comfy, catalogue, comfy_settings):
        catalogue["A"] = _picture(1)
        make_comfy()
        make_comfy(key="pim.broken", workflow="missing.json")
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{COMFY}/system_stats").mock(return_value=httpx.Response(200, json={}))
            summary = comfy_spawn_task()
        assert summary == {"spawned": ["pim.packshot"], "skipped": [], "failed": ["pim.broken"]}
