# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The bus's door to a ComfyUI server (`ENRICHMENT_COMFYUI_URL`, the plain ComfyUI HTTP API, no auth) — the `comfy`
kind's only transport, next to the AI toolbox, not through it: ComfyUI runs in the client's own network.

One run = upload the input (`/upload/image`), queue the workflow (`/prompt`), poll `/history/<id>` until it is there,
download the first output image (`/view`). Every failure is a `SubjectError` with a stable code, a timeout is a
`TimeoutError` (`TIMEOUT`, transient); nothing here ever answers in ComfyUI's place.
"""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from django.conf import settings

from django_enrichment.kinds.base import SubjectError
from django_enrichment.services import lease_keepalive

POLL_INTERVAL_S = 1
_HTTP_TIMEOUT_S = 30
_DEFAULT_TIMEOUT_S = 180
NOT_CONFIGURED = "COMFYUI_NOT_CONFIGURED"
UNAVAILABLE = "COMFYUI_UNAVAILABLE"
FAILED = "COMFYUI_FAILED"
NO_OUTPUT = "COMFYUI_NO_OUTPUT"


def run(workflow: dict, *, input_node: str, image: bytes, filename: str) -> bytes:
    """Run `workflow` (ComfyUI API format) on `image`, fed to the `LoadImage` node `input_node`; the first output
    image's bytes. The workflow dict is not changed."""
    with _errors(), _client() as client:
        name = _upload(client, image, filename)
        prompt = {**workflow, input_node: _with_image(workflow[input_node], name)}
        prompt_id = _ok(client.post("prompt", json={"prompt": prompt, "client_id": uuid.uuid4().hex})).json()
        entry = _wait(client, prompt_id["prompt_id"])
        return _ok(client.get("view", params=_first_image(entry))).content


def available() -> bool:
    """True when the server answers `/system_stats` — the spawn's cheap look before it queues work."""
    try:
        with _client() as client:
            return client.get("system_stats").is_success
    except (SubjectError, httpx.HTTPError):
        return False


def _client() -> httpx.Client:
    base_url = getattr(settings, "ENRICHMENT_COMFYUI_URL", "")
    if not base_url:
        raise SubjectError(NOT_CONFIGURED, "ENRICHMENT_COMFYUI_URL is not set")
    return httpx.Client(base_url=f"{base_url.rstrip('/')}/", timeout=_HTTP_TIMEOUT_S)


@contextmanager
def _errors() -> Iterator[None]:
    """httpx failures → a stable code: a timeout is transient (`TIMEOUT`), anything else at the transport level means
    the server is not there. The httpx error stays chained for the log."""
    try:
        yield
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"ComfyUI timed out: {type(exc).__name__}") from exc
    except httpx.HTTPError as exc:
        raise SubjectError(UNAVAILABLE, f"ComfyUI not reachable: {type(exc).__name__}") from exc


def _ok(response: httpx.Response) -> httpx.Response:
    if not response.is_success:
        raise SubjectError(f"COMFYUI_HTTP_{response.status_code}", f"ComfyUI answered {response.status_code}")
    return response


def _upload(client: httpx.Client, image: bytes, filename: str) -> str:
    """The name ComfyUI stored the input under (`subfolder/name` when it put it in one)."""
    body = _ok(client.post("upload/image", files={"image": (filename, image)}, data={"overwrite": "true"})).json()
    return f"{body['subfolder']}/{body['name']}" if body.get("subfolder") else body["name"]


def _with_image(node: dict, name: str) -> dict:
    return {**node, "inputs": {**node.get("inputs", {}), "image": name}}


def _wait(client: httpx.Client, prompt_id: str) -> dict:
    """Poll the history until the prompt is in it (finished, either way); an error status raises `COMFYUI_FAILED`."""
    timeout_s = getattr(settings, "ENRICHMENT_COMFYUI_TIMEOUT_S", _DEFAULT_TIMEOUT_S)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        lease_keepalive.beat()  # a queued prompt can wait behind other jobs on a shared GPU
        if entry := _ok(client.get(f"history/{prompt_id}")).json().get(prompt_id):
            if (entry.get("status") or {}).get("status_str") == "error":
                raise SubjectError(FAILED, f"ComfyUI prompt {prompt_id} failed")
            return entry
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"ComfyUI prompt {prompt_id} not done after {timeout_s}s")


def _first_image(entry: dict) -> dict:
    """`/view` params of the first image any output node saved (nodes in id order)."""
    outputs = entry.get("outputs") or {}
    for node_id in sorted(outputs, key=_node_order):
        for image in outputs[node_id].get("images") or []:
            return {"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image["type"]}
    raise SubjectError(NO_OUTPUT, "the workflow saved no image")


def _node_order(node_id: str) -> tuple[int, str]:
    return (int(node_id), "") if node_id.isdigit() else (1 << 30, node_id)
