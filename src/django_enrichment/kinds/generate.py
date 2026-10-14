# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `generate` — one free-form output (markdown, text or JSON) per subject, as an artifact.

One completion from the brief, the needed steps' values and the profile/augmentation context
(`kinds.step`). `markdown` / `text` → value `{"text"}`; `json` → value `{"data"}` shaped by `json_schema`
(an object; any object when absent). The draft is an artifact of the built-in `enrichment` adapter.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from django_enrichment.kinds.base import Kind, ProposalDraft, with_cost
from django_enrichment.kinds.step import StepParams, build_prompt, locator
from django_enrichment.services import toolbox_calls

TARGET_KIND = "artifact"
_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
_FORMAT_NOTES = {
    "markdown": "Answer with the whole output as markdown in the `text` property.",
    "text": "Answer with the whole output as plain text (no markdown) in the `text` property.",
    "json": "Answer with one JSON object that follows the schema.",
}


class GenerateOptions(BaseModel):
    rules: list[str] = Field(default_factory=list, description="Rules every run follows", examples=[["No prices."]])


class GenerateParams(StepParams):
    format: Literal["markdown", "text", "json"] = Field("markdown", description="Output format")
    json_schema: dict | None = Field(None, description="Schema of a json output (an object)")

    @field_validator("json_schema")
    @classmethod
    def _object_schema(cls, schema: dict | None) -> dict | None:
        if schema is not None and schema.get("type") != "object":
            raise ValueError('json_schema must describe an object ("type": "object")')
        return schema


class GenerateKind(Kind):
    Options = GenerateOptions
    Params = GenerateParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:  # noqa: ARG002
        params = GenerateParams.model_validate(task.params)
        params.rules = [*GenerateOptions.model_validate(worker.options).rules, *params.rules]
        prompt = build_prompt(worker, task, params, kind="generate", parts=[_FORMAT_NOTES[params.format]])
        answer = toolbox_calls.complete(
            system=prompt.system,
            user=prompt.user,
            json_schema=_schema(params),
            model=worker.model,
            kind="generate",
            task=task,
        )
        draft = ProposalDraft(
            target_kind=TARGET_KIND, target_locator=locator(params), proposed_value=_value(params, answer.value)
        )
        return with_cost([draft], answer.cost)


def _schema(params: GenerateParams) -> dict:
    if params.format != "json":
        return _TEXT_SCHEMA
    return params.json_schema or {"type": "object"}


def _value(params: GenerateParams, answer: dict) -> dict:
    return {"data": answer} if params.format == "json" else {"text": str(answer.get("text", ""))}
