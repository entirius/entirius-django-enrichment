# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `video` — a short product video from the subject's main picture (Amendment A1).

The picture comes from the adapter's optional `main_picture(*, subject_ref, channel) -> (bytes, mime)`
(looked up with `getattr`; absent → the subject fails). The toolbox media job runs to completion inside
this call (blocking, POC). One media draft per subject: `target_kind="video"`, locator `{channel}`; the
runner stages the mp4 through `proposal_service.intake_media`.
"""

from typing import Any, Literal

from django.core.files.base import ContentFile
from pydantic import BaseModel, Field

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds.base import Kind, ProposalDraft, join_prompts
from django_enrichment.services import toolbox_calls

TARGET_KIND = "video"


class VideoOptions(BaseModel):
    preset: str = Field(min_length=1, description="Toolbox video preset key", examples=["product-orbit"])
    duration_s: Literal[4, 6, 8] = Field(4, description="Video length in seconds", examples=[4])
    aspect: Literal["9:16", "16:9"] = Field("9:16", description="Output aspect ratio", examples=["9:16"])


class VideoParams(BaseModel):
    channel: str = Field(min_length=1, description="Channel idx", examples=["default"])
    prompt: str = Field("", description="Extra instructions for this run", examples=["Wooden table, soft light."])


class VideoKind(Kind):
    Options = VideoOptions
    Params = VideoParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        options = VideoOptions.model_validate(worker.options)
        params = VideoParams.model_validate(task.params)
        main_picture = getattr(get_adapter(worker.target_module), "main_picture", None)
        if main_picture is None:
            raise ValueError(f"adapter {worker.target_module!r} has no main_picture")
        image, mime = main_picture(subject_ref=subject_ref, channel=params.channel)
        video = toolbox_calls.generate_video(image=image, mime=mime, form=_form(options, worker, params))
        value = {"preset": options.preset, "duration_s": options.duration_s, "aspect": options.aspect}
        media = ContentFile(video, name="video.mp4")
        return [ProposalDraft(TARGET_KIND, {"channel": params.channel}, value, media_file=media)]


def _form(options: VideoOptions, worker: Any, params: VideoParams) -> dict[str, str]:
    return {
        "preset": options.preset,
        "prompt": join_prompts(worker.system_prompt, params.prompt),
        "aspect_ratio": options.aspect,
        "duration_s": str(options.duration_s),
    }
