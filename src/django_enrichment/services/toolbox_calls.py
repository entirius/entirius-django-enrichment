"""The bus's only door to the AI toolbox — completion, translation and video jobs.

Completion and translation go through `django_utils.toolbox.ToolboxClient` (transport, retries, typed
errors) on the instance channel. Video jobs talk to the toolbox media API with plain httpx: the endpoint
may differ from the text toolbox (`ENRICHMENT_VIDEO_TOOLBOX_*`, POC escape hatch, Redmine #34464).
Prompts live in the kinds. Toolbox errors propagate: the runner counts the subject as failed.

Every call returns `Costed(value, cost)` and charges its task (`cost_service.charge`); `cost_of` is the one
place that knows which response field carries the cost today. Completion tags: the selector
`enrichment.<kind>[.<template>]` first (the toolbox fake provider picks fixtures by it), then `task:<id>`.
"""

import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from django.conf import settings
from django_utils.toolbox import CompletionRequest, Message, ToolboxClient, ToolboxError
from django_utils.toolbox import settings as toolbox_settings

from django_enrichment import settings as enrichment_settings
from django_enrichment.kinds.base import SubjectError
from django_enrichment.services import cost_service

_TRANSLATOR_TOOL = "ai-translator"
_VIDEO_POLL_INTERVAL_S = 10
_VIDEO_HTTP_TIMEOUT_S = 60.0
# Where each toolbox response carries its cost today (switch here for a unified cost endpoint).
_COST_FIELDS = {"completion": "cost", "translate": "total_cost_usd", "media": "cost", "seo": "cost"}
_TAG_INVALID = re.compile(r"[^a-z0-9_.:-]")
_TAG_MAX = 64


@dataclass(frozen=True)
class Costed:
    """A toolbox result with the cost the toolbox reported for it (`None` = not reported)."""

    value: Any
    cost: Decimal | None


def cost_of(source: str, body: dict) -> Decimal | None:
    """The cost of one toolbox response of `source` (`completion | translate | media | seo`); absent → `None`."""
    raw = body.get(_COST_FIELDS[source])
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def complete(*, system: str, user: str, json_schema: dict, model: str, kind: str, task: Any = None) -> Costed:
    """One structured completion for `task` (tags + cost); `value` is the parsed answer.

    Blank `model` = the channel's first catalogue model. `kind` names the selector tag."""
    messages = [Message(role="system", content=system)] if system else []
    messages.append(Message(role="user", content=user))
    with ToolboxClient() as client:
        request = CompletionRequest(
            model=model or _default_model(client), messages=messages, json_schema=json_schema, tags=_tags(kind, task)
        )
        response = client.complete(request)
    cost = _charge(task, "completion", response.model_dump())
    if response.parsed is None:
        raise ValueError("toolbox completion returned no structured answer")
    return Costed(response.parsed, cost)


def _tags(kind: str, task: Any) -> list[str]:
    """`["enrichment.<kind>[.<template>]", "task:<id>"]` — the selector always first."""
    template = (getattr(task, "params", None) or {}).get("template") or ""
    tags = [_tag(".".join(part for part in ("enrichment", kind, template) if part))]
    if (task_id := getattr(task, "pk", None)) is not None:
        tags.append(f"task:{task_id}")
    return tags


def _tag(text: str) -> str:
    return _TAG_INVALID.sub("-", text.lower())[:_TAG_MAX]


def _charge(task: Any, source: str, body: dict) -> Decimal | None:
    cost = cost_of(source, body)
    cost_service.charge(task, source, cost)
    return cost


def translate(
    texts: dict[str, str], *, source: str, target: str, context: str, options: dict, task: Any = None
) -> Costed:
    """Translate `{key: text}` in one call (`context_mode` — keys are field names); `value` = `{key: translation}`.

    `ToolboxClient` has no translator method, so this posts through its request helpers — paid and
    non-idempotent, hence `retry=False` (re-sent only when the request never left or on a 429).
    """
    payload = _translate_payload(texts, source, target, context, options)
    with ToolboxClient() as client:
        body = client._post(client._url(_TRANSLATOR_TOOL, "translate/"), payload, retry=False)
    cost = _charge(task, "translate", body)
    return Costed({item["key"]: item["translated_text"] for item in body["items"]}, cost)


def _translate_payload(texts: dict[str, str], source: str, target: str, context: str, options: dict) -> dict:
    payload = {
        "items": [{"key": key, "text": text} for key, text in texts.items()],
        "source_language": source,
        "target_language": target,
        "context_mode": True,
        "context": context or None,
        "provider": options.get("provider") or None,
        "formality": options.get("formality") or None,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _default_model(client: ToolboxClient) -> str:
    models = client.list_models()
    if not models:
        raise ValueError("toolbox channel has no completion model on its allow-list")
    return models[0].model_id


def generate_video(*, image: bytes, mime: str, form: dict[str, str], task: Any = None) -> Costed:
    """Image-to-video on the toolbox media API: submit → poll until done → download the mp4 (`value`).

    `form` carries `preset`, `prompt`, `aspect_ratio`, `duration_s`. Polls inside the caller (a Celery
    task) up to `ENRICHMENT_VIDEO_TIMEOUT_S`; a failed job or the timeout raises. The cost is the done job's.
    """
    with _video_client() as client:
        response = client.post("videos/", data=form, files={"image": ("image", image, mime)})
        job_id = response.raise_for_status().json()["job_id"]
        job = _wait_for_video(client, job_id)
        video = client.get(f"videos/{job_id}/download/").raise_for_status().content
    return Costed(video, _charge(task, "media", job))


def _wait_for_video(client: httpx.Client, job_id: str) -> dict:
    timeout_s = getattr(settings, "ENRICHMENT_VIDEO_TIMEOUT_S", enrichment_settings.ENRICHMENT_VIDEO_TIMEOUT_S)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = client.get(f"videos/{job_id}/").raise_for_status().json()
        if job["status"] == "done":
            return job
        if job["status"] == "failed":
            code = (job.get("error") or {}).get("error", "UNKNOWN")
            raise ValueError(f"toolbox video job {job_id} failed: {code}")
        time.sleep(_VIDEO_POLL_INTERVAL_S)
    raise TimeoutError(f"toolbox video job {job_id} not done after {timeout_s}s")


def _video_client() -> httpx.Client:
    base_url = _video_setting("URL", "AI_TOOLBOX_BASE_URL").rstrip("/")
    api_key = _video_setting("API_KEY", "AI_TOOLBOX_API_KEY")
    channel = _video_setting("CHANNEL", "AI_TOOLBOX_CHANNEL")
    if not (base_url and api_key and channel):
        raise ValueError("video toolbox is not configured (ENRICHMENT_VIDEO_TOOLBOX_* / AI_TOOLBOX_*)")
    return httpx.Client(
        base_url=f"{base_url}/api/ai-media/v2/admin/{channel}/",
        headers={"X-API-Key": api_key},
        timeout=_VIDEO_HTTP_TIMEOUT_S,
    )


def _video_setting(suffix: str, fallback: str) -> str:
    """`ENRICHMENT_VIDEO_TOOLBOX_<suffix>`, blank or unset → the matching `AI_TOOLBOX_*` setting."""
    return getattr(settings, f"ENRICHMENT_VIDEO_TOOLBOX_{suffix}", "") or getattr(toolbox_settings, fallback)


def failure_code(exc: BaseException) -> str:
    """A stable, operator-safe code for a failed subject: the toolbox error code, else a generic class.

    Never the exception text — a toolbox message may quote model output, an internal one leaks internals.
    """
    if isinstance(exc, SubjectError):
        return exc.code
    if isinstance(exc, ToolboxError):
        return exc.code or f"TOOLBOX_HTTP_{exc.status_code}"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    return "INTERNAL_ERROR"
