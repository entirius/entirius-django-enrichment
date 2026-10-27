# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Workers, templates and definitions for the workflow tests, plus a scripted toolbox answer.

The document step writes to the fake adapter registered as `"docs"` (it creates documents on apply).
"""

import json

import httpx
from django_utils.toolbox.testing import CANNED_COMPLETION

from django_enrichment.models import TaskTemplate, Worker, WorkflowDefinition
from django_enrichment.services import template_service, worker_service

DOC_MODULE = "docs"
COST = "0.01"
META_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "slug": {"type": "string"}, "description": {"type": "string"}},
    "required": ["title", "slug", "description"],
}
QUESTIONS = [
    {"id": "years", "question": "Since when?", "why": "Proof", "type": "text", "choices": [], "optional": False},
    {
        "id": "tone",
        "question": "Tone?",
        "why": "Voice",
        "type": "choice",
        "choices": ["warm", "dry"],
        "optional": False,
    },
    {"id": "extra", "question": "Anything else?", "why": "", "type": "text", "choices": [], "optional": True},
]
META = {"title": "Linen sofa care", "slug": "Linen Sofa Care", "description": "How to keep linen fresh.", "faq": []}
_WORKERS = {
    "wf.ask": ("questions", "enrichment"),
    "wf.write": ("generate", "enrichment"),
    "wf.compose": ("compose", DOC_MODULE),
}
_TEMPLATES = {
    "ask": ("wf.ask", {"prompt": "Ask about the topic."}),
    "outline": ("wf.write", {"prompt": "Write an outline."}),
    "draft": ("wf.write", {"prompt": "Write the article."}),
    "meta": ("wf.write", {"prompt": "Write the meta.", "format": "json", "json_schema": META_SCHEMA}),
    "compose": ("wf.compose", {}),
}


def make_templates() -> None:
    for key, (kind, module) in _WORKERS.items():
        worker_service.save_worker(Worker(key=key, label=key, kind=kind, target_module=module, system_prompt="Sys."))
    for key, (worker_key, params) in _TEMPLATES.items():
        worker = Worker.objects.get(key=worker_key)
        template_service.save_template(TaskTemplate(key=key, label=key, worker=worker, params=params))


def step(key: str, template: str = "", needs: tuple = (), gate: str = "review", output: str = "artifact") -> dict:
    return {
        "key": key,
        "label": key.title(),
        "template": template or key,
        "needs": list(needs),
        "gate": gate,
        "output": output,
    }


def make_definition(key: str, steps: list[dict], inputs_schema: dict | None = None) -> WorkflowDefinition:
    definition = WorkflowDefinition(
        key=key, label=key.title(), target_module=DOC_MODULE, steps=steps, inputs_schema=inputs_schema or {}
    )
    definition.full_clean()
    definition.save()
    return definition


def respond(request: httpx.Request) -> httpx.Response:
    """A schema-shaped answer: questions, `{"text"}` or the meta object."""
    properties = json.loads(request.content)["json_schema"].get("properties", {})
    if "questions" in properties:
        parsed = {"questions": QUESTIONS}
    elif "text" in properties:
        parsed = {"text": "# Linen sofa care\n\nBrush it weekly."}
    else:
        parsed = META
    return httpx.Response(200, json={**CANNED_COMPLETION, "parsed": parsed, "cost": COST})


def respond_with(parsed: dict):
    """A toolbox answer that always carries `parsed` (what a model wrote, escapes and all)."""
    return lambda request: httpx.Response(200, json={**CANNED_COMPLETION, "parsed": parsed, "cost": COST})


def sent(route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]
