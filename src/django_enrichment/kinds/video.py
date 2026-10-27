# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `video` — a short product video from one of the subject's pictures (Amendment A1).

Without `params.picture_ref` the picture comes from the adapter's optional
`main_picture(*, subject_ref, channel) -> (bytes, mime)`; with it, from the optional
`picture(*, subject_ref, channel, picture_ref) -> (bytes, mime)` (a ref from `list_pictures`), which raises
`LookupError` when the picture is not the subject's → `SubjectError("PICTURE_NOT_FOUND")`; either one raising `OSError`
(file gone, not an image) → `SubjectError("PICTURE_UNREADABLE")`. Both are looked up with `getattr`; absent → the
subject fails. The CMS sends `picture_ref` only for a one-product run. The toolbox media job runs to completion inside
this call (blocking, POC). One media draft per subject: `target_kind="video"`, locator `{channel}`; the
runner stages the mp4 through `proposal_service.intake_media`.
"""

from typing import Any, Literal

from django.core.files.base import ContentFile
from pydantic import BaseModel, Field

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds.base import (
    Kind,
    ProposalDraft,
    SubjectError,
    join_prompts,
    unreadable_picture,
    with_cost,
)
from django_enrichment.services import toolbox_calls

TARGET_KIND = "video"


class VideoOptions(BaseModel):
    preset: str = Field(min_length=1, description="Toolbox video preset key", examples=["product-orbit"])
    duration_s: Literal[4, 6, 8] = Field(4, description="Video length in seconds", examples=[4])
    aspect: Literal["9:16", "16:9"] = Field("9:16", description="Output aspect ratio", examples=["9:16"])


class VideoParams(BaseModel):
    channel: str = Field(min_length=1, description="Channel idx", examples=["default"])
    prompt: str = Field("", description="Extra instructions for this run", examples=["Wooden table, soft light."])
    picture_ref: str = Field(
        "", max_length=64, description="Picture to animate (`subjects/pictures/` ref); blank = the main picture"
    )


class VideoKind(Kind):
    Options = VideoOptions
    Params = VideoParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        options = VideoOptions.model_validate(worker.options)
        params = VideoParams.model_validate(task.params)
        image, mime = _source_picture(worker.target_module, subject_ref, params)
        video = toolbox_calls.generate_video(image=image, mime=mime, form=_form(options, worker, params), task=task)
        value = {"preset": options.preset, "duration_s": options.duration_s, "aspect": options.aspect}
        media = ContentFile(video.value, name="video.mp4")
        return with_cost([ProposalDraft(TARGET_KIND, {"channel": params.channel}, value, media_file=media)], video.cost)


def _source_picture(target_module: str, subject_ref: str, params: VideoParams) -> tuple[bytes, str]:
    """The chosen picture (`params.picture_ref`) or, without one, the subject's main picture; a file that cannot be
    read (`OSError`: gone from disk, not an image) fails the subject with `PICTURE_UNREADABLE`."""
    try:
        return _read_picture(target_module, subject_ref, params)
    except OSError as exc:
        raise unreadable_picture(subject_ref) from exc


def _read_picture(target_module: str, subject_ref: str, params: VideoParams) -> tuple[bytes, str]:
    if not params.picture_ref:
        return _extra(target_module, "main_picture")(subject_ref=subject_ref, channel=params.channel)
    try:
        return _extra(target_module, "picture")(
            subject_ref=subject_ref, channel=params.channel, picture_ref=params.picture_ref
        )
    except LookupError as exc:
        raise SubjectError(
            "PICTURE_NOT_FOUND", f"picture {params.picture_ref!r} is not one of {subject_ref!r}'s"
        ) from exc


def _extra(target_module: str, name: str) -> Any:
    if (extra := getattr(get_adapter(target_module), name, None)) is None:
        raise ValueError(f"adapter {target_module!r} has no {name}")
    return extra


def _form(options: VideoOptions, worker: Any, params: VideoParams) -> dict[str, str]:
    return {
        "preset": options.preset,
        "prompt": join_prompts(worker.system_prompt, params.prompt),
        "aspect_ratio": options.aspect,
        "duration_s": str(options.duration_s),
    }
