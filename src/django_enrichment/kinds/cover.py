# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `cover` — a workflow's cover picture: one image per scene of the scenes step, the operator picks one.

The scenes come from the `{"data"}` input of `scenes_step` (`{scenes: [{title, prompt, alt, must_show}]}`, written by
a `generate` step; `must_show` = what the article demands the picture gets right, e.g. a safe position). Each scene is one toolbox image job (`instruct`, one output, `aspect_ratio`). Its input picture is the main
picture of the product the brief names (`product_field` = `{channel, ref}`, a subject of `product_module`), else a
blank canvas: the toolbox edits pictures, it has no text-to-image operation. Code, not the model, ends every prompt —
the scene's `must_show` (`Must show:`, never cut), the profile's `visual` section (capped, so the scene keeps its room),
the cover text the brief asks for or a ban on any text, a regeneration's instruction — so every cover of a series follows the same rules.

The images travel as one staged bundle (`image_bundle`); the value is `{variants: [{title, prompt, alt, must_show}], chosen,
alt, cover_text}` — `alt` starts as the first variant's, the CMS moves it with the pick. The operator sets `chosen` (and may fix
`alt`); approval refuses an artifact without a valid pick. A scene
whose job fails is left out; all of them failing fails the subject.
"""

import struct
import zlib
from decimal import Decimal
from typing import Any, Literal

from django.core.exceptions import ObjectDoesNotExist
from django.core.files.base import ContentFile
from pydantic import BaseModel, Field

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds import seams
from django_enrichment.kinds.base import Kind, ProposalDraft, SubjectError
from django_enrichment.kinds.step import StepParams, locator
from django_enrichment.services import image_bundle, toolbox_calls

TARGET_KIND = "artifact"
PROMPT_MAX = 1000  # the toolbox image prompt cap
VISUAL_SECTION = "visual"
LEAD_CANVAS = (
    "The input is only a blank placeholder canvas: ignore it and replace the whole frame with a new image, full-bleed "
    "from edge to edge, with no border, frame or margin."
)
LEAD_PRODUCT = (
    "Use the product from the input photo as the exact product: same shape, colours, proportions and details. "
    "Show it naturally in the new scene, at true scale, never as a cut-out."
)
NO_TEXT = "No text, letters, numbers, logos or watermarks anywhere in the image."
STYLE_MAX = 300  # the profile's visual text in a prompt; the never-show list gets NEVER_MAX
NEVER_MAX = 150
MUST_MAX = 250  # a scene's must-show conditions in a prompt
_CANVAS_SIZES = {"16:9": (1344, 768), "4:3": (1184, 864), "3:2": (1248, 832), "1:1": (1024, 1024)}
_CANVAS_RGB = (238, 232, 224)


class CoverNotChosenError(ValueError):
    """Approval of a cover artifact without a valid pick (API: 400 `COVER_NOT_CHOSEN`)."""


class CoverOptions(BaseModel):
    """No worker-level options: the template presets the params."""


class CoverParams(StepParams):
    scenes_step: str = Field("cover_brief", description="Step whose data holds the scenes")
    aspect_ratio: Literal["16:9", "4:3", "3:2", "1:1"] = Field("16:9", description="Shape of the cover")
    max_variants: int = Field(3, ge=1, le=image_bundle.MAX_VARIANTS, description="Most scenes rendered")
    product_field: str = Field("cover_product", description="Brief key of the product `{channel, ref}`")
    product_module: str = Field("pim", description="Adapter of the product")
    text_flag: str = Field("cover_text_wanted", description="Brief key: true = the cover carries a text")
    text_field: str = Field("cover_text", description="Brief key of that text")


class CoverKind(Kind):
    Options = CoverOptions
    Params = CoverParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:  # noqa: ARG002
        params = CoverParams.model_validate(task.params)
        scenes = _scenes(params)
        image, mime, lead = _input_picture(params)
        tail = _tail(task, params)
        images, cost = _render(task, scenes, image=image, mime=mime, lead=lead, tail=tail, params=params)
        variants = [scene for scene, _ in images]
        value = {"variants": variants, "chosen": None, "alt": variants[0]["alt"], "cover_text": cover_text(params)}
        bundle = ContentFile(image_bundle.pack([png for _, png in images]), name="cover.zip")
        draft = ProposalDraft(TARGET_KIND, locator(params), value, subject_label="Cover", media_file=bundle)
        draft.cost_usd = cost
        return [draft]


def validate_pick(value: dict) -> None:
    """Raises `CoverNotChosenError` unless `chosen` names one of the variants."""
    chosen = value.get("chosen")
    count = len(value.get("variants") or [])
    if not isinstance(chosen, int) or isinstance(chosen, bool) or not 0 <= chosen < count:
        raise CoverNotChosenError("choose one of the cover variants before approving")


def cover_text(params: CoverParams) -> str:
    """The text the brief wants on the cover; blank when its flag is off."""
    if params.brief.get(params.text_flag) is not True:
        return ""
    return str(params.brief.get(params.text_field) or "").strip()


def _scenes(params: CoverParams) -> list[dict]:
    data = (params.inputs.get(params.scenes_step) or {}).get("data") or {}
    scenes = [_scene(scene) for scene in data.get("scenes") or [] if isinstance(scene, dict)]
    scenes = [scene for scene in scenes if scene["prompt"]][: params.max_variants]
    if not scenes:
        raise SubjectError("INPUT_MISSING", f"no scenes from step {params.scenes_step!r}")
    return scenes


def _scene(scene: dict) -> dict:
    must = scene.get("must_show") if isinstance(scene.get("must_show"), list) else []
    texts = {key: str(scene.get(key) or "").strip() for key in ("title", "prompt", "alt")}
    return texts | {"must_show": [item for item in (str(entry).strip() for entry in must) if item]}


def must_show(scene: dict) -> str:
    """`Must show: …` of a scene (capped), blank without conditions."""
    conditions = _cut("; ".join(scene.get("must_show") or []), MUST_MAX)
    return f"Must show: {conditions}." if conditions else ""


def _input_picture(params: CoverParams) -> tuple[bytes, str, str]:
    """`(bytes, mime, lead)`: the product's main picture, or a blank canvas of the cover's shape."""
    product = params.brief.get(params.product_field)
    if not isinstance(product, dict) or not product.get("ref"):
        return _canvas(params.aspect_ratio), "image/png", LEAD_CANVAS
    adapter = get_adapter(params.product_module)
    ref, channel = str(product["ref"]), str(product.get("channel") or "")
    try:
        pictures = adapter.list_pictures(subject_ref=ref, channel=channel)
        main = next((item["ref"] for item in pictures if item.get("is_main")), None)
        if main is None:
            raise LookupError(f"product {ref!r} has no main picture")
        image, mime = adapter.picture(subject_ref=ref, channel=channel, picture_ref=main)
    except (LookupError, ObjectDoesNotExist) as exc:
        raise SubjectError("PRODUCT_PICTURE_MISSING", str(exc)) from exc
    return image, mime, LEAD_PRODUCT


def _tail(task: Any, params: CoverParams) -> str:
    """What code adds after every scene: the profile's visual style, the text rule, the operator's instruction."""
    visual = seams.profile_section(task, channel=params.channel, name=VISUAL_SECTION)
    style = _cut(str(visual.get("text") or "").strip(), STYLE_MAX)
    never = _cut(
        ", ".join(str(item).strip() for item in visual.get("never_show") or [] if str(item).strip()), NEVER_MAX
    )
    text = cover_text(params)
    parts = [
        f"Style: {style}" if style else "",
        f"Never show: {never}." if never else "",
        f'Text on the image, exactly as written: "{text}". Clean, legible typography; no other text.'
        if text
        else NO_TEXT,
        f"Change: {params.instruction.strip()}" if params.instruction.strip() else "",
    ]
    return " ".join(part for part in parts if part)


def _render(
    task: Any, scenes: list[dict], *, image: bytes, mime: str, lead: str, tail: str, params: CoverParams
) -> tuple[list[tuple[dict, bytes]], Decimal | None]:
    """One image job per scene: `([(scene, png)], summed cost)` — a failed scene is left out, all failing raises."""
    images, costs, error = [], [], None
    for scene in scenes:
        form = {
            "operation": "instruct",
            "prompt": fit_prompt(lead, scene["prompt"], " ".join(filter(None, (must_show(scene), tail)))),
            "n": "1",
            "aspect_ratio": params.aspect_ratio,
        }
        try:
            result = toolbox_calls.generate_image(image=image, mime=mime, form=form, task=task)
        except SubjectError as exc:
            error = exc
            continue
        images.append((scene, result.value[0]))
        costs.append(result.cost)
    if not images:
        raise error or SubjectError("COVER_FAILED", "no cover variant was made")
    return images, None if None in costs else sum(costs, Decimal(0))


def fit_prompt(lead: str, scene: str, tail: str) -> str:
    """`lead scene tail` within `PROMPT_MAX`: the scene is shortened (at a word) when the three do not fit."""
    scene = _cut(scene, PROMPT_MAX - len(lead) - len(tail) - 2)
    return " ".join(part for part in (lead, scene, tail) if part)[:PROMPT_MAX]


def _cut(text: str, limit: int) -> str:
    """`text` within `limit` characters, shortened at a word with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rsplit(" ", 1)[0] + "…"


def _canvas(aspect_ratio: str) -> bytes:
    """A plain warm-grey PNG of the cover's shape (no imaging library needed)."""
    width, height = _CANVAS_SIZES[aspect_ratio]
    raw = zlib.compress((b"\x00" + bytes(_CANVAS_RGB) * width) * height, 9)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", raw) + _chunk(b"IEND", b"")


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
