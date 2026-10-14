# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `write` — write several text fields of an item, one completion per subject × language.

The answer is one JSON object with a string property per field (`maxLength` from the field catalog);
`html` fields are sanitised with the field kind's allowlist. One draft per field × language, located
with the adapter's `make_locator`. Select fields belong to `pick` → subject error `FIELD_TYPE`.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds import extras
from django_enrichment.kinds.base import Kind, ProposalDraft, SubjectError, join_prompts, with_cost
from django_enrichment.kinds.field import sanitize_html
from django_enrichment.services import toolbox_calls

TARGET_KIND = "attribute_value"


class WriteOptions(BaseModel):
    rules: list[str] = Field(default_factory=list, description="Rules every run follows", examples=[["No prices."]])
    field_rules: dict[str, str] = Field(
        default_factory=dict, description="Extra rule per field key", examples=[{"meta_title": "Brand first."}]
    )


class WriteParams(BaseModel):
    channel: str = Field(min_length=1, description="Channel idx", examples=["default"])
    fields: list[str] = Field(min_length=1, description="Field keys to write", examples=[["meta_title"]])
    languages: list[str] = Field(min_length=1, description='Languages; ["*"] = all channel languages', examples=[["*"]])
    prompt: str = Field("", description="Extra instructions for this run", examples=["Mention the warranty."])
    rules: list[str] = Field(default_factory=list, description="Rules of this run", examples=[["Use 'you'."]])


class WriteKind(Kind):
    Options = WriteOptions
    Params = WriteParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        params = WriteParams.model_validate(task.params)
        adapter = get_adapter(worker.target_module)
        run = _WriteRun(worker, task, WriteOptions.model_validate(worker.options), params, adapter)
        languages = extras.languages(adapter, channel=params.channel, requested=params.languages)
        return [draft for language in languages for draft in run.drafts(subject_ref, language)]


@dataclass
class _WriteRun:
    worker: Any
    task: Any
    options: WriteOptions
    params: WriteParams
    adapter: Any

    def drafts(self, subject_ref: str, language: str) -> list[ProposalDraft]:
        """One completion for the subject in `language` → one draft per field."""
        catalog = extras.catalog(self.adapter, channel=self.params.channel, language=language)
        _check_types(self.params.fields, catalog)
        answer = toolbox_calls.complete(
            system=self.worker.system_prompt,
            user=self._user_message(subject_ref, language, catalog),
            json_schema=_answer_schema(self.params.fields, catalog),
            model=self.worker.model,
            kind="write",
            task=self.task,
        )
        drafts = [self._draft(language, key, _clean(answer.value[key], catalog.get(key))) for key in self.params.fields]
        return with_cost(drafts, answer.cost)

    def _user_message(self, subject_ref: str, language: str, catalog: dict[str, dict]) -> str:
        channel = self.params.channel
        return join_prompts(
            extras.rules_block([*self.options.rules, *self.params.rules]),
            self.params.prompt,
            extras.context_block(self.adapter, subject_ref=subject_ref, channel=channel, language=language),
            _fields_block(self.params.fields, catalog, self.options.field_rules),
            f'Write every field in language "{language}". Answer as one JSON object with one string per field key.',
        )

    def _draft(self, language: str, key: str, text: str) -> ProposalDraft:
        locator = extras.locator(self.adapter, field_key=key, channel=self.params.channel, language=language)
        return ProposalDraft(target_kind=TARGET_KIND, target_locator=locator, proposed_value={"text": text})


def _check_types(fields: list[str], catalog: dict[str, dict]) -> None:
    if wrong := [key for key in fields if catalog.get(key, {}).get("type") in extras.SELECT_TYPES]:
        raise SubjectError("FIELD_TYPE", f"select fields cannot be written: {wrong}")


def _fields_block(fields: list[str], catalog: dict[str, dict], field_rules: dict[str, str]) -> str:
    return "Fields to write:\n" + "\n".join(
        _field_line(key, catalog.get(key), field_rules.get(key, "")) for key in fields
    )


def _field_line(key: str, meta: dict | None, rule: str) -> str:
    meta = meta or {}
    parts = [f'- "{key}": {extras.label(meta, key)}', meta.get("type") or "text"]
    if meta.get("max_length"):
        parts.append(f"at most {meta['max_length']} characters")
    if meta.get("type") == "html":
        parts.append("raw HTML tags such as <p>, not escaped entities")
    if rule:
        parts.append(rule)
    return "; ".join(parts)


def _answer_schema(fields: list[str], catalog: dict[str, dict]) -> dict:
    properties = {key: _property(catalog.get(key)) for key in fields}
    return {"type": "object", "properties": properties, "required": list(fields), "additionalProperties": False}


def _property(meta: dict | None) -> dict:
    prop: dict = {"type": "string"}
    if max_length := (meta or {}).get("max_length"):
        prop["maxLength"] = max_length
    return prop


def _clean(value: str, meta: dict | None) -> str:
    return sanitize_html(value) if (meta or {}).get("type") == "html" else value
