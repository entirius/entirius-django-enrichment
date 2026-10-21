# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `cover`: one picture per scene of the scenes step, staged as one bundle; the operator picks one, approval
needs the pick, compose carries the picked picture to the document. Plus the brief's product (start check, facts
augmentation), the profile's visual section and the bundle's variant endpoint."""

import base64
import io
import json
import zipfile
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.base import ContentFile
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.augmentations import cover_product
from django_enrichment.kinds import compose as compose_kind
from django_enrichment.kinds import cover as cover_kind
from django_enrichment.kinds.base import SubjectError
from django_enrichment.kinds.cover import NO_TEXT, CoverKind, CoverNotChosenError, fit_prompt, validate_pick
from django_enrichment.models import ContentProposal, EnrichmentTask, TaskTemplate, Worker, WorkflowRun
from django_enrichment.services import (
    image_bundle,
    profile_context,
    profile_service,
    proposal_service,
    template_service,
    toolbox_calls,
    worker_service,
    workflow_queries,
    workflow_service,
    workflow_steps,
)
from django_enrichment.workflow_spec import step_errors
from tests import workflow_setup as wf

_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
SCENES = {
    "data": {
        "scenes": [
            {
                "title": "Morning",
                "prompt": "A sleepy baby in mum's arms by a window.",
                "alt": "A baby asleep",
                "must_show": ["baby held semi-upright", " "],
            },
            {"title": "Cot", "prompt": "The soother on a knit blanket next to a small hand.", "alt": "A soother"},
        ]
    }
}
PROPOSALS = "/api/enrichment/v2/admin/proposals/"
COVER_VALUE = {"variants": [{"title": "A", "prompt": "a"}, {"title": "B", "prompt": "b"}], "chosen": None, "alt": "x"}
pytestmark = pytest.mark.django_db


@pytest.fixture
def staging_dir(settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    return tmp_path


@pytest.fixture
def jobs(monkeypatch):
    """`toolbox_calls.generate_image` answered here: one PNG per job (`_PNG` + the job number); records the calls.
    Prompts containing "BLOCKED" fail like a blocked image."""
    calls = []

    def _generate(*, image, mime, form, task=None):
        calls.append({"image": image, "mime": mime, "form": form})
        if "BLOCKED" in form["prompt"]:
            raise SubjectError("IMAGE_BLOCKED")
        return toolbox_calls.Costed([_PNG + bytes([len(calls)])], Decimal("0.039"))

    monkeypatch.setattr(toolbox_calls, "generate_image", _generate)
    return calls


@pytest.fixture
def product(fake_adapter, monkeypatch):
    """The fake adapter's product "SKU-1" (channel "default") with main picture "5"; others are unknown."""

    def _pictures(*, subject_ref, channel):
        if subject_ref != "SKU-1":
            raise ObjectDoesNotExist(subject_ref)
        return [{"ref": "3", "is_main": False}, {"ref": "5", "is_main": True}]

    def _picture(*, subject_ref, channel, picture_ref):
        return b"product-photo-" + picture_ref.encode(), "image/jpeg"

    monkeypatch.setattr(fake_adapter, "list_pictures", _pictures, raising=False)
    monkeypatch.setattr(fake_adapter, "picture", _picture, raising=False)
    return fake_adapter


def _run(params: dict) -> list:
    worker = SimpleNamespace(system_prompt="", model="", options={})
    task = EnrichmentTask.objects.create(type="wf.cover", params={"step": "cover", **params})
    return CoverKind().run(worker, task, "run:abc")


def _bundle(draft) -> list[bytes]:
    with zipfile.ZipFile(io.BytesIO(draft.media_file.read())) as bundle:
        return [bundle.read(name) for name in sorted(bundle.namelist())]


class TestBundle:
    def test_pack_and_read_keep_the_variants_in_order(self):
        packed = image_bundle.pack([b"one", b"two"])
        assert image_bundle.read(io.BytesIO(packed), 1) == b"two"

    def test_a_missing_variant_or_a_file_that_is_no_bundle_is_a_lookup_error(self):
        with pytest.raises(LookupError):
            image_bundle.read(io.BytesIO(image_bundle.pack([b"one"])), 3)
        with pytest.raises(LookupError):
            image_bundle.read(io.BytesIO(_PNG), 0)


class TestCoverKind:
    def test_one_job_per_scene_on_a_blank_canvas_of_the_cover_shape(self, jobs):
        _run({"inputs": {"cover_brief": SCENES}})
        assert len(jobs) == 2
        assert jobs[0]["image"].startswith(b"\x89PNG") and jobs[0]["mime"] == "image/png"
        assert {call["form"]["aspect_ratio"] for call in jobs} == {"16:9"}
        assert {call["form"]["operation"] for call in jobs} == {"instruct"}
        assert jobs[0]["form"]["prompt"].startswith(cover_kind.LEAD_CANVAS)
        assert "A sleepy baby" in jobs[0]["form"]["prompt"]

    def test_the_value_lists_the_variants_unpicked_and_the_bundle_holds_their_pictures(self, jobs):
        (draft,) = _run({"inputs": {"cover_brief": SCENES}})
        assert draft.target_kind == "artifact" and draft.target_locator == {"step": "cover"}
        assert draft.proposed_value == {
            "variants": [
                {**SCENES["data"]["scenes"][0], "must_show": ["baby held semi-upright"]},
                {**SCENES["data"]["scenes"][1], "must_show": []},
            ],
            "chosen": None,
            "alt": "A baby asleep",
            "cover_text": "",
        }
        assert _bundle(draft) == [_PNG + b"\x01", _PNG + b"\x02"]
        assert draft.cost_usd == Decimal("0.078")

    def test_the_must_show_conditions_follow_the_scene_and_are_never_cut(self, jobs):
        long_scene = {**SCENES["data"]["scenes"][0], "prompt": "word " * 400}
        _run({"inputs": {"cover_brief": {"data": {"scenes": [long_scene, SCENES["data"]["scenes"][1]]}}}})
        first, second = (call["form"]["prompt"] for call in jobs)
        assert "Must show: baby held semi-upright." in first and len(first) <= cover_kind.PROMPT_MAX
        assert "Must show" not in second

    def test_no_text_is_the_rule_unless_the_brief_asks_for_one(self, jobs):
        _run({"inputs": {"cover_brief": SCENES}, "brief": {"cover_text": "Ignored"}})
        assert NO_TEXT in jobs[0]["form"]["prompt"] and "Ignored" not in jobs[0]["form"]["prompt"]

    def test_the_cover_text_of_the_brief_is_written_into_every_prompt(self, jobs):
        (draft,) = _run(
            {"inputs": {"cover_brief": SCENES}, "brief": {"cover_text_wanted": True, "cover_text": "Sleep"}}
        )
        assert all('exactly as written: "Sleep"' in call["form"]["prompt"] for call in jobs)
        assert NO_TEXT not in jobs[0]["form"]["prompt"]
        assert draft.proposed_value["cover_text"] == "Sleep"

    def test_the_profile_visual_section_and_the_instruction_end_every_prompt(self, jobs):
        profile_service.save_profile(
            channel="", data={"visual": {"text": "Warm editorial photo.", "never_show": ["bottles", "logos"]}}
        )
        _run({"inputs": {"cover_brief": SCENES}, "instruction": "No yellow cushion."})
        prompt = jobs[1]["form"]["prompt"]
        assert prompt.endswith("Change: No yellow cushion.")
        assert "Style: Warm editorial photo." in prompt and "Never show: bottles, logos." in prompt

    def test_a_long_visual_style_is_capped_so_the_scene_keeps_its_room(self, jobs):
        profile_service.save_profile(channel="", data={"visual": {"text": "soft light " * 200}})
        _run({"inputs": {"cover_brief": SCENES}})
        prompt = jobs[0]["form"]["prompt"]
        assert len(prompt) <= cover_kind.PROMPT_MAX
        assert "A sleepy baby in mum's arms by a window." in prompt and prompt.endswith(NO_TEXT)

    def test_the_product_main_picture_is_the_input_when_the_brief_names_one(self, jobs, product):
        _run({"inputs": {"cover_brief": SCENES}, "brief": {"cover_product": {"channel": "default", "ref": "SKU-1"}}})
        assert jobs[0]["image"] == b"product-photo-5" and jobs[0]["mime"] == "image/jpeg"
        assert jobs[0]["form"]["prompt"].startswith(cover_kind.LEAD_PRODUCT)

    def test_a_product_gone_since_the_start_fails_the_subject(self, jobs, product):
        with pytest.raises(SubjectError) as exc:
            _run({"inputs": {"cover_brief": SCENES}, "brief": {"cover_product": {"channel": "default", "ref": "OLD"}}})
        assert exc.value.code == "PRODUCT_PICTURE_MISSING"

    def test_a_failed_scene_is_left_out_and_all_failing_fail_the_subject(self, jobs):
        scenes = {"data": {"scenes": [{"title": "X", "prompt": "BLOCKED"}, SCENES["data"]["scenes"][0]]}}
        (draft,) = _run({"inputs": {"cover_brief": scenes}})
        assert [v["title"] for v in draft.proposed_value["variants"]] == ["Morning"]
        with pytest.raises(SubjectError) as exc:
            _run({"inputs": {"cover_brief": {"data": {"scenes": [{"title": "X", "prompt": "BLOCKED"}]}}}})
        assert exc.value.code == "IMAGE_BLOCKED"

    def test_no_scenes_fail_the_subject_and_extra_scenes_are_cut(self, jobs):
        with pytest.raises(SubjectError) as exc:
            _run({"inputs": {}})
        assert exc.value.code == "INPUT_MISSING"
        many = {"data": {"scenes": [{"title": str(n), "prompt": f"scene {n}"} for n in range(5)]}}
        (draft,) = _run({"inputs": {"cover_brief": many}, "max_variants": 3})
        assert len(draft.proposed_value["variants"]) == 3


class TestPromptAndPick:
    def test_a_long_scene_is_shortened_so_the_prompt_fits_the_toolbox_cap(self):
        prompt = fit_prompt("Lead.", "word " * 400, "Tail.")
        assert len(prompt) <= cover_kind.PROMPT_MAX
        assert prompt.startswith("Lead.") and prompt.endswith("Tail.")

    @pytest.mark.parametrize("chosen", [None, -1, 2, True, "0"])
    def test_a_pick_outside_the_variants_is_refused(self, chosen):
        with pytest.raises(CoverNotChosenError):
            validate_pick({**COVER_VALUE, "chosen": chosen})

    def test_a_valid_pick_passes(self):
        validate_pick({**COVER_VALUE, "chosen": 1})


def _staged_cover(subject_ref: str = "post:1", value: dict | None = None) -> ContentProposal:
    bundle = ContentFile(image_bundle.pack([_PNG + b"a", _PNG + b"b"]), name="cover.zip")
    return proposal_service.intake_media(
        file=bundle,
        target_module="enrichment",
        subject_ref=subject_ref,
        target_kind="artifact",
        target_locator={"step": "cover"},
        proposed_value=value or dict(COVER_VALUE),
    )


class TestCoverApi:
    def test_a_variant_is_served_from_the_staged_bundle(self, admin_client, staging_dir):
        proposal = _staged_cover()
        response = admin_client.get(f"{PROPOSALS}{proposal.pk}/staged-file/?variant=1")
        assert response.status_code == 200 and response["Content-Type"] == "image/png"
        assert response.content == _PNG + b"b"
        assert admin_client.get(f"{PROPOSALS}{proposal.pk}/staged-file/?variant=7").status_code == 404
        assert admin_client.get(f"{PROPOSALS}{proposal.pk}/staged-file/?variant=x").status_code == 404

    def test_a_zip_is_staged_for_an_artifact_only(self, staging_dir):
        with pytest.raises(ValueError, match="unsupported image type"):
            proposal_service.intake_media(
                file=ContentFile(image_bundle.pack([_PNG]), name="x.zip"),
                target_module="enrichment",
                subject_ref="post:1",
                target_kind="picture",
                target_locator={"step": "cover"},
                proposed_value={},
            )

    def test_approval_without_a_pick_is_a_400_cover_not_chosen(self, admin_client, staging_dir):
        proposal = _staged_cover()
        response = admin_client.post(f"{PROPOSALS}{proposal.pk}/accept/", {}, format="json")
        assert response.status_code == 400 and response.json()["error"] == "COVER_NOT_CHOSEN"

    def test_an_approved_cover_keeps_its_bundle_for_compose(self, admin_client, staging_dir):
        proposal = _staged_cover(value={**COVER_VALUE, "chosen": 0})
        assert admin_client.post(f"{PROPOSALS}{proposal.pk}/accept/", {}, format="json").status_code == 200
        proposal.refresh_from_db()
        assert proposal.status == "applied" and (staging_dir / proposal.staged_file).exists()


class TestStartChecksTheProduct:
    SCHEMA = {
        "type": "object",
        "properties": {"cover_product": {"type": "object", "x-picture-of": "pim"}},
    }

    @pytest.fixture
    def definition(self, product, settings):
        settings.ENRICHMENT_ADAPTERS = {"pim": "tests.fake_adapter", wf.DOC_MODULE: "tests.fake_adapter"}
        wf.make_templates()
        return wf.make_definition("cover-start", [wf.step("outline")], self.SCHEMA)

    def _start(self, product_value: dict):
        return workflow_service.start(definition="cover-start", inputs={"cover_product": product_value})

    def test_a_product_with_a_main_picture_starts(self, definition):
        assert self._start({"channel": "default", "ref": "SKU-1"}).uid

    def test_an_unknown_product_is_refused_at_the_start(self, definition):
        with pytest.raises(ValueError, match="inputs.cover_product: 'NOPE' not found"):
            self._start({"channel": "default", "ref": "NOPE"})

    def test_a_product_without_a_main_picture_is_refused_at_the_start(self, definition, product, monkeypatch):
        monkeypatch.setattr(product, "list_pictures", lambda **_kw: [{"ref": "3", "is_main": False}])
        with pytest.raises(ValueError, match="has no main picture"):
            self._start({"channel": "default", "ref": "SKU-1"})


class TestBriefPerStep:
    def test_a_field_with_x_steps_reaches_only_the_steps_it_lists(self, fake_adapter, settings):
        settings.ENRICHMENT_ADAPTERS = {"pim": "tests.fake_adapter", wf.DOC_MODULE: "tests.fake_adapter"}
        wf.make_templates()
        schema = {"type": "object", "properties": {"topic": {"type": "string"}, "cover_hint": {"x-steps": ["cover"]}}}
        definition = wf.make_definition("per-step", [wf.step("outline")], schema)
        run = WorkflowRun(definition=definition, inputs={"topic": "Sofa", "cover_hint": "Warm light"})
        assert workflow_steps.brief_for(run, "outline") == {"topic": "Sofa"}
        assert workflow_steps.brief_for(run, "cover") == {"topic": "Sofa", "cover_hint": "Warm light"}


class TestCoverProductAugmentation:
    def test_the_product_facts_in_the_brief_language(self, product):
        result = cover_product.run(
            task=None,
            subject_ref="run:x",
            context={"brief": {"cover_product": {"channel": "default", "ref": "SKU-1"}}, "language": "pl"},
        )
        assert json.loads(result.block)["sku"] == "SKU-1" and json.loads(result.block)["language"] == "pl"

    def test_no_product_no_block(self, product):
        assert cover_product.run(task=None, subject_ref="run:x", context={"brief": {}}).block == ""


class TestVisualProfile:
    def test_the_visual_section_renders_with_its_never_show_list(self):
        data = {"visual": {"text": "Soft daylight photos.", "never_show": ["logos"]}}
        instructions, _ = profile_context.render(data, task_type="t", kind="generate", sections=["visual"])
        assert "## Visual style\nSoft daylight photos." in instructions and "Never show:\n- logos" in instructions

    def test_step_sections_replace_the_kind_routing_but_not_the_task_type_routing(self):
        data = {"routing": {"article.cover_brief": ["brand"]}}
        assert profile_context.routed_sections({}, task_type="t", kind="generate", sections=["visual"]) == ["visual"]
        assert profile_context.routed_sections(
            data, task_type="article.cover_brief", kind="generate", sections=["visual"]
        ) == ["brand"]


SCENES_SCHEMA = {
    "type": "object",
    "properties": {"scenes": {"type": "array"}},
    "required": ["scenes"],
}


def _respond(request: httpx.Request) -> httpx.Response:
    if "scenes" in json.loads(request.content)["json_schema"].get("properties", {}):
        return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": SCENES["data"], "cost": wf.COST})
    return wf.respond(request)


class TestCoverWorkflow:
    @pytest.fixture
    def definition(self, fake_adapter, settings, staging_dir):
        settings.ENRICHMENT_ADAPTERS = {"pim": "tests.fake_adapter", wf.DOC_MODULE: "tests.fake_adapter"}
        wf.make_templates()
        for key, kind in (("wf.cover", "cover"),):
            worker_service.save_worker(Worker(key=key, label=key, kind=kind, target_module="enrichment"))
        write = Worker.objects.get(key="wf.write")
        template_service.save_template(
            TaskTemplate(
                key="scenes",
                label="scenes",
                worker=write,
                params={"prompt": "Write scenes.", "format": "json", "json_schema": SCENES_SCHEMA},
            )
        )
        template_service.save_template(
            TaskTemplate(
                key="cover",
                label="cover",
                worker=Worker.objects.get(key="wf.cover"),
                params={"scenes_step": "cover_brief"},
            )
        )
        template_service.save_template(
            TaskTemplate(
                key="compose-cover",
                label="c",
                worker=Worker.objects.get(key="wf.compose"),
                params={"body_step": "draft", "cover_step": "cover"},
            )
        )
        steps = [
            wf.step("draft", gate="auto"),
            wf.step("meta", needs=("draft",), gate="auto"),
            wf.step("cover_brief", template="scenes", needs=("draft",), gate="auto"),
            wf.step("cover", needs=("cover_brief",), output="image"),
            wf.step("compose", template="compose-cover", needs=("draft", "meta", "cover"), output="document"),
        ]
        return wf.make_definition("covered", steps, {"type": "object"})

    def test_the_picked_variant_reaches_the_document_draft(
        self, definition, jobs, admin_user, django_capture_on_commit_callbacks, toolbox_settings
    ):
        with mock_toolbox() as router:
            router["complete"].mock(side_effect=_respond)
            with django_capture_on_commit_callbacks(execute=True):
                run = workflow_service.start(definition="covered", inputs={}, channel="default", user=admin_user)
            cover = workflow_queries.step_proposal(str(run.uid), "cover")
            assert cover.staged_file and cover.proposed_value["chosen"] is None
            proposal_service.update_value(cover, {**cover.proposed_value, "chosen": 1, "alt": "Fixed alt"})
            with django_capture_on_commit_callbacks(execute=True):
                proposal_service.accept(cover, user=admin_user)
        compose = workflow_queries.step_proposal(str(run.uid), "compose")
        assert compose.proposed_value["cover"] == {"alt": "Fixed alt", "file_name": "linen-sofa-care-cover.png"}
        with compose.open_staged_file() as staged:
            assert staged.read() == _PNG + b"\x02"

    def test_a_blank_alt_falls_back_to_the_picked_variant_alt(self, staging_dir, monkeypatch):
        cover = _staged_cover(value={**COVER_VALUE, "variants": SCENES["data"]["scenes"], "chosen": 1, "alt": ""})
        monkeypatch.setattr(workflow_queries, "step_proposal", lambda _uid, _key: cover)
        params = SimpleNamespace(inputs={"cover": cover.proposed_value}, run="x", cover_step="cover")
        value, _ = compose_kind.cover(params, "post")
        assert value == {"alt": "A soother", "file_name": "post-cover.png"}

    def test_an_image_step_needs_the_review_gate_and_a_cover_worker(self, definition):
        auto = [wf.step("cover", gate="auto", output="image")]
        assert "an image step needs the review gate" in " ".join(step_errors(auto, wf.DOC_MODULE))
        wrong = [wf.step("cover", template="scenes", output="image")]
        assert "needs a 'cover' worker" in " ".join(step_errors(wrong, wf.DOC_MODULE))
