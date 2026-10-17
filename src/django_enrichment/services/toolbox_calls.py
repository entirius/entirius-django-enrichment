"""The bus's only door to the AI toolbox — completion, translation, SEO data, video/image jobs and the usage read.

Completion, translation and SEO data go through `django_utils.toolbox.ToolboxClient` (transport, retries, typed
errors) on the instance channel. Video and image jobs talk to the toolbox media API with plain httpx: the endpoint
may differ from the text toolbox (`ENRICHMENT_VIDEO_TOOLBOX_*`, POC escape hatch, Redmine #34464).
Prompts live in the kinds. Toolbox errors propagate: the runner counts the subject as failed.

The schema sent to the provider is the "portable" subset (`provider_schema`): the provider cannot enforce value
limits (length, item count, range) and the toolbox answers 502 when a model answer breaks one, so the limits
travel as description hints and the schema keeps only structure.

Every call returns `Costed(value, cost)` and charges its task (`cost_service.charge`); `cost_of` is the one
place that knows which response field carries the cost today. Completion tags: the selector
`enrichment.<kind>[.<template>]` first (the toolbox fake provider picks fixtures by it), then `task:<id>` and,
for a workflow step, `run:<uid>`.
A call that fails after its request left may already be billed: it charges an unknown cost (`None`), so the
task is never reported complete with a too-low total.
"""

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from django.conf import settings
from django_utils.toolbox import CompletionRequest, Message, ToolboxClient, ToolboxError, ToolboxNotFoundError
from django_utils.toolbox import settings as toolbox_settings

from django_enrichment import settings as enrichment_settings
from django_enrichment.kinds.base import SubjectError
from django_enrichment.services import cost_service

_TRANSLATOR_TOOL = "ai-translator"
_SEO_TOOL = "ai-seo"
_TRACKER_TOOL = "ai-tracker"
_USAGE_READ_TIMEOUT_S = 5.0
_VIDEO_POLL_INTERVAL_S = 10
_IMAGE_POLL_INTERVAL_S = 3
_VIDEO_HTTP_TIMEOUT_S = 60.0
# Where each toolbox response carries its cost today (switch here for a unified cost endpoint).
_COST_FIELDS = {"completion": "cost", "translate": "total_cost_usd", "media": "cost", "seo": "cost"}
_TAG_INVALID = re.compile(r"[^a-z0-9_.:-]")
_TAG_MAX = 64
_LIMIT_HINTS = {
    "minLength": "min {} chars",
    "maxLength": "max {} chars",
    "minItems": "min {} items",
    "maxItems": "max {} items",
    "minimum": "min {}",
    "maximum": "max {}",
}
_DROPPED_KEYWORDS = frozenset(
    {"exclusiveMinimum", "exclusiveMaximum", "multipleOf", "pattern", "format", "uniqueItems"}
)
_DETAIL_MAX = 300
_COST_STEP = Decimal("0.00000001")  # the ledger columns: decimal_places=8
_COST_LIMIT = Decimal(10) ** 6  # the ledger columns: max_digits=14 → below 10**6


@dataclass(frozen=True)
class Costed:
    """A toolbox result with the cost the toolbox reported for it (`None` = not reported)."""

    value: Any
    cost: Decimal | None


def cost_of(source: str, body: dict) -> Decimal | None:
    """The cost of one toolbox response of `source` (`completion | translate | media | seo`), at ledger precision.

    Absent, unparsable, non-finite, negative or out of the ledger's range → `None` (unknown, never 0)."""
    raw = body.get(_COST_FIELDS[source])
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not value.is_finite() or not 0 <= value < _COST_LIMIT:
        return None
    return value.quantize(_COST_STEP)


@contextmanager
def _unknown_cost_on_error(task: Any, source: str) -> Iterator[None]:
    """A failure once the request left may still be billed → charge an unknown cost, then re-raise."""
    try:
        yield
    except Exception:
        cost_service.charge(task, source, None)
        raise


def complete(*, system: str, user: str, json_schema: dict, model: str, kind: str, task: Any = None) -> Costed:
    """One structured completion for `task` (tags + cost); `value` is the parsed answer.

    Blank `model` = the channel's first catalogue model. `kind` names the selector tag."""
    messages = [Message(role="system", content=system)] if system else []
    messages.append(Message(role="user", content=user))
    with ToolboxClient() as client:
        request = CompletionRequest(
            model=model or _default_model(client),
            messages=messages,
            json_schema=provider_schema(json_schema),
            max_tokens=_max_tokens(),
            tags=_tags(kind, task),
        )
        with _unknown_cost_on_error(task, "completion"):
            response = client.complete(request)
    cost = _charge(task, "completion", response.model_dump())
    if response.parsed is None:
        raise ValueError("toolbox completion returned no structured answer")
    return Costed(response.parsed, cost)


def _max_tokens() -> int:
    default = enrichment_settings.ENRICHMENT_COMPLETION_MAX_TOKENS
    return getattr(settings, "ENRICHMENT_COMPLETION_MAX_TOKENS", default)


def provider_schema(schema: Any) -> Any:
    """`schema` without value limits and format keywords; each limit is kept as a hint in `description`."""
    if isinstance(schema, list):
        return [provider_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    hints = [text.format(schema[key]) for key, text in _LIMIT_HINTS.items() if key in schema]
    result = {key: _portable_value(key, value) for key, value in schema.items() if _keeps(key)}
    if hints:
        description = result.get("description")
        result["description"] = f"{description} ({', '.join(hints)})" if description else f"({', '.join(hints)})"
    return result


def _keeps(key: str) -> bool:
    return key not in _LIMIT_HINTS and key not in _DROPPED_KEYWORDS


def _portable_value(key: str, value: Any) -> Any:
    """`properties` / `$defs` map names to schemas — a property called `format` must survive."""
    if key in ("properties", "$defs", "definitions", "patternProperties") and isinstance(value, dict):
        return {name: provider_schema(sub) for name, sub in value.items()}
    return value if key in ("enum", "const", "required", "examples", "default") else provider_schema(value)


def _tags(kind: str, task: Any) -> list[str]:
    """`["enrichment.<kind>[.<template>]", "task:<id>", "run:<uid>"]` — the selector always first."""
    params = getattr(task, "params", None) or {}
    template = params.get("template") or ""
    tags = [_tag(".".join(part for part in ("enrichment", kind, template) if part))]
    if (task_id := getattr(task, "pk", None)) is not None:
        tags.append(f"task:{task_id}")
    if run := params.get("run"):
        tags.append(_tag(f"run:{run}"))
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
        with _unknown_cost_on_error(task, "translate"):
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


def seo(endpoint: str, payload: dict, *, task: Any = None) -> Costed:
    """One toolbox SEO call (`search-volume/`, `keyword-ideas/`, `serp/`); `value` = the envelope's `data` (None
    when missing — the caller validates it).

    Posted like translate (paid, `retry=False`); a toolbox cache hit comes back with cost 0."""
    with ToolboxClient() as client:
        with _unknown_cost_on_error(task, "seo"):
            body = client._post(client._url(_SEO_TOOL, endpoint), payload, retry=False)
    cost = _charge(task, "seo", body)
    return Costed(body.get("data"), cost)


def tracker_month(year_month: str) -> tuple[dict, dict | None]:
    """The instance client's toolbox usage for one UTC month `YYYY-MM`: `(usage, billing)`.

    `usage/` = provider cost and units per tool and provider; `billing/client/` = what the client is billed
    (provider cost plus the toolbox markup), `None` while billing is off for the client (404). Read-only: one
    attempt each, short timeout — a header counter must not hang on a toolbox that is down."""
    year, month = year_month.split("-")
    with ToolboxClient(max_retries=1) as client:
        usage = client._get(client._url(_TRACKER_TOOL, "usage/"), {"year": year, "month": month}, _USAGE_READ_TIMEOUT_S)
        try:
            billing = client._get(
                client._url(_TRACKER_TOOL, "billing/client/"),
                {"year_month": year_month, "granularity": "none"},
                _USAGE_READ_TIMEOUT_S,
            )
        except ToolboxNotFoundError:
            billing = None
    return usage, billing


def _default_model(client: ToolboxClient) -> str:
    models = client.list_models()
    if not models:
        raise ValueError("toolbox channel has no completion model on its allow-list")
    return models[0].model_id


def generate_video(*, image: bytes, mime: str, form: dict[str, str], task: Any = None) -> Costed:
    """Image-to-video on the toolbox media API: submit → poll until done → download the mp4 (`value`).

    `form` carries `preset`, `prompt`, `aspect_ratio`, `duration_s`. Polls inside the caller (a Celery
    task) up to `ENRICHMENT_VIDEO_TIMEOUT_S`; a failed job or the timeout raises. The finished job's cost is
    charged before the download (a failed job's too); polling that breaks off charges an unknown cost.
    """
    with _media_client() as client:
        job_id, job, cost = _run_media_job(client, _VIDEO_JOB, image=(image, mime), form=form, task=task)
        video = _checked(client.get(f"videos/{job_id}/download/")).content
    return Costed(video, cost)


def generate_image(*, image: bytes, mime: str, form: dict[str, str], task: Any = None) -> Costed:
    """Image processing on the toolbox media API: submit → poll → download every output (`value` = list of PNG bytes).

    `form` carries `operation`, `preset` (optional), `prompt`, `n`. Same job flow and error/cost rules as
    `generate_video`; polls up to `ENRICHMENT_IMAGE_TIMEOUT_S`. The job's cost covers all its outputs.
    """
    with _media_client() as client:
        job_id, job, cost = _run_media_job(client, _IMAGE_JOB, image=(image, mime), form=form, task=task)
        outputs = sorted(output["index"] for output in job.get("outputs") or [])
        images = [_checked(client.get(f"images/{job_id}/outputs/{k}/download/")).content for k in outputs]
    if not images:
        raise ValueError(f"toolbox image job {job_id} finished without outputs")
    return Costed(images, cost)


@dataclass(frozen=True)
class _MediaJob:
    path: str  # collection under the media API: `videos` | `images`
    timeout_setting: str
    poll_interval_s: int


_VIDEO_JOB = _MediaJob("videos", "ENRICHMENT_VIDEO_TIMEOUT_S", _VIDEO_POLL_INTERVAL_S)
_IMAGE_JOB = _MediaJob("images", "ENRICHMENT_IMAGE_TIMEOUT_S", _IMAGE_POLL_INTERVAL_S)


def _run_media_job(
    client: httpx.Client, spec: _MediaJob, *, image: tuple[bytes, str], form: dict[str, str], task: Any
) -> tuple[str, dict, Decimal | None]:
    """Submit one media job and wait for it: `(job_id, finished job, charged cost)`; a failed job raises."""
    data, mime = image
    response = client.post(f"{spec.path}/", data=form, files={"image": ("image", data, mime)})
    job_id = _checked(response).json()["job_id"]
    with _unknown_cost_on_error(task, "media"):
        job = _wait_for_job(client, spec, job_id)
    cost = _charge(task, "media", job)
    if job["status"] == "failed":  # the toolbox's code (IMAGE_BLOCKED, PROVIDER_ERROR) is the subject's error code
        code = (job.get("error") or {}).get("error", "UNKNOWN")
        raise SubjectError(code, f"toolbox {spec.path[:-1]} job {job_id} failed: {code}")
    return job_id, job, cost


def _checked(response: httpx.Response) -> httpx.Response:
    """The response, or a `ToolboxError` carrying the toolbox's own error code and message (plain httpx calls)."""
    if response.is_success:
        return response
    try:
        body = response.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    raise ToolboxError(
        response.status_code,
        response.reason_phrase,
        str(body.get("error") or ""),
        upstream_message=str(body.get("message") or ""),
    )


def _wait_for_job(client: httpx.Client, spec: _MediaJob, job_id: str) -> dict:
    """Poll until the job is `done` or `failed` (returned either way); the timeout raises."""
    timeout_s = getattr(settings, spec.timeout_setting, getattr(enrichment_settings, spec.timeout_setting))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = _checked(client.get(f"{spec.path}/{job_id}/")).json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(spec.poll_interval_s)
    raise TimeoutError(f"toolbox {spec.path[:-1]} job {job_id} not done after {timeout_s}s")


def _media_client() -> httpx.Client:
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


def failure_detail(exc: BaseException) -> str:
    """What the toolbox said about a failed call — `HTTP <status> <code>: <message>` (capped), `""` for other errors.

    The toolbox message is its own provider-error text (no keys, no request body); a gateway page that is not
    JSON leaves it blank, which the detail says instead of dropping it."""
    if not isinstance(exc, ToolboxError):
        return ""
    message = " ".join(exc.upstream_message.split()) or "no message (non-JSON or empty response body)"
    return f"HTTP {exc.status_code} {exc.code or '-'}: {message}"[:_DETAIL_MAX]


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
