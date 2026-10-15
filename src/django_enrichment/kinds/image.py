# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `image` — process one of the subject's pictures (packshot, lifestyle, enhance, free instruction).

The source picture is `params.picture_ref` (a ref from the adapter's optional `list_pictures`), blank = the
one `list_pictures` marks `is_main`. It is read with the optional `picture(*, subject_ref, channel, picture_ref)`
(`LookupError` → `SubjectError("PICTURE_NOT_FOUND")`); an adapter without both extras fails the subject. The
toolbox image job runs to completion inside this call (blocking, like `video`). Each output becomes one media
draft `target_kind="picture"`, locator `{channel, source_picture_ref, operation, variant}` — the source ref tells
the adapter which picture "replace" swaps; operation + variant keep the variants of one run apart (a re-run of
the same operation supersedes its own variants only). The job's cost is split over the drafts.
"""

from typing import Any, Literal

from django.core.files.base import ContentFile
from pydantic import BaseModel, Field, model_validator

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds.base import Kind, ProposalDraft, SubjectError, join_prompts, with_cost
from django_enrichment.services import toolbox_calls

TARGET_KIND = "picture"
DERIVED_OP = "derived"
PROMPT_MAX = 1000


class ImageOptions(BaseModel):
    """No worker-level options: what to do comes with each run (templates preset it)."""


class ImageParams(BaseModel):
    channel: str = Field(min_length=1, description="Channel idx", examples=["default"])
    operation: Literal["packshot", "lifestyle", "enhance", "instruct"] = Field(
        description="What to do with the picture", examples=["packshot"]
    )
    preset: str = Field("", max_length=64, description="Toolbox image preset; blank = the toolbox default")
    prompt: str = Field(
        "",
        max_length=PROMPT_MAX,
        description="Instruction for this run (required for `instruct`)",
        examples=["Put the mug on a wooden kitchen table."],
    )
    picture_ref: str = Field(
        "", max_length=64, description="Picture to process (`subjects/pictures/` ref); blank = the main picture"
    )
    n: int = Field(1, ge=1, le=4, description="How many variants to make", examples=[2])

    @model_validator(mode="after")
    def _instruct_needs_a_prompt(self) -> "ImageParams":
        # A template (no channel yet) may leave the instruction to the run; a run must carry it.
        if self.operation == "instruct" and self.channel and not self.prompt.strip():
            raise ValueError("a custom instruction needs a prompt")
        return self


class ImageKind(Kind):
    Options = ImageOptions
    Params = ImageParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        params = ImageParams.model_validate(task.params)
        adapter = get_adapter(worker.target_module)
        source_ref = params.picture_ref or _main_ref(adapter, subject_ref, params.channel)
        image, mime = _read_picture(adapter, subject_ref, params.channel, source_ref)
        result = toolbox_calls.generate_image(image=image, mime=mime, form=_form(worker, params), task=task)
        drafts = [_draft(params, source_ref, index, png, len(result.value)) for index, png in enumerate(result.value)]
        return with_cost(drafts, result.cost)


def _extra(adapter: Any, name: str) -> Any:
    if (extra := getattr(adapter, name, None)) is None:
        raise ValueError(f"adapter {adapter.__name__!r} has no {name}")
    return extra


def _main_ref(adapter: Any, subject_ref: str, channel: str) -> str:
    pictures = _extra(adapter, "list_pictures")(subject_ref=subject_ref, channel=channel)
    if main := next((item["ref"] for item in pictures if item.get("is_main")), None):
        return main
    raise SubjectError("NO_PICTURE", f"{subject_ref!r} has no main picture")


def _read_picture(adapter: Any, subject_ref: str, channel: str, picture_ref: str) -> tuple[bytes, str]:
    try:
        return _extra(adapter, "picture")(subject_ref=subject_ref, channel=channel, picture_ref=picture_ref)
    except LookupError as exc:
        raise SubjectError("PICTURE_NOT_FOUND", f"picture {picture_ref!r} is not one of {subject_ref!r}'s") from exc


def _form(worker: Any, params: ImageParams) -> dict[str, str]:
    form = {
        "operation": params.operation,
        "prompt": join_prompts(worker.system_prompt, params.prompt),
        "n": str(params.n),
    }
    if params.preset:
        form["preset"] = params.preset
    return form


def _draft(params: ImageParams, source_ref: str, index: int, png: bytes, count: int) -> ProposalDraft:
    locator = {
        "channel": params.channel,
        "source_picture_ref": source_ref,
        "operation": params.operation,
        "variant": index + 1,
    }
    value = {
        "op": DERIVED_OP,
        "operation": params.operation,
        "preset": params.preset,
        "variant": index + 1,
        "of": count,
    }
    return ProposalDraft(TARGET_KIND, locator, value, media_file=ContentFile(png, name=f"image-{index + 1}.png"))
