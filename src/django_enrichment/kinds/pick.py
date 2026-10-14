# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `pick` — choose values of select / multiselect fields, one completion per subject.

The answer schema carries an enum of the catalog option values per field (an array of them for
multiselect). One draft per field, locator language `None`: `{"option": value}` or
`{"options": [values]}`. A field that is not a select type (or no catalog) → subject error `FIELD_TYPE`.
"""

from typing import Any

from pydantic import BaseModel, Field

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds import extras, seams
from django_enrichment.kinds.base import Kind, ProposalDraft, SubjectError, join_prompts, with_cost
from django_enrichment.services import toolbox_calls

TARGET_KIND = "attribute_value"


class PickOptions(BaseModel):
    rules: list[str] = Field(default_factory=list, description="Rules every run follows", examples=[["Be strict."]])


class PickParams(BaseModel):
    channel: str = Field(min_length=1, description="Channel idx", examples=["default"])
    fields: list[str] = Field(min_length=1, description="Select field keys", examples=[["color"]])
    prompt: str = Field("", description="Extra instructions for this run", examples=["Prefer basic colours."])
    rules: list[str] = Field(default_factory=list, description="Rules of this run", examples=[["One colour only."]])


class PickKind(Kind):
    Options = PickOptions
    Params = PickParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        options = PickOptions.model_validate(worker.options)
        params = PickParams.model_validate(task.params)
        adapter = get_adapter(worker.target_module)
        fields = _select_fields(params.fields, extras.catalog(adapter, channel=params.channel))
        language = extras.main_language(adapter, channel=params.channel)
        instructions, profile = seams.profile_for_task(task, channel=params.channel, kind="pick")
        user = join_prompts(
            extras.rules_block([*options.rules, *params.rules]),
            params.prompt,
            extras.context_block(adapter, subject_ref=subject_ref, channel=params.channel, language=language),
            seams.memory_block(channel=params.channel, subject_module=worker.target_module, subject_ref=subject_ref),
            seams.profile_block(profile),
            _fields_block(fields),
        )
        answer = toolbox_calls.complete(
            system=join_prompts(worker.system_prompt, instructions),
            user=user,
            json_schema=_answer_schema(fields),
            model=worker.model,
            kind="pick",
            task=task,
        )
        return with_cost(
            [_draft(adapter, params.channel, meta, answer.value[meta["key"]]) for meta in fields], answer.cost
        )


def _select_fields(keys: list[str], catalog: dict[str, dict]) -> list[dict]:
    fields = [catalog.get(key) or {"key": key} for key in keys]
    if wrong := [meta["key"] for meta in fields if not _pickable(meta)]:
        raise SubjectError("FIELD_TYPE", f"not select fields with options: {wrong}")
    return fields


def _pickable(meta: dict) -> bool:
    return meta.get("type") in extras.SELECT_TYPES and bool(meta.get("options"))


def _fields_block(fields: list[dict]) -> str:
    lines = "\n".join(_field_line(meta) for meta in fields)
    return f"Choose values for these fields (use the option values):\n{lines}\nAnswer as one JSON object with one property per field key."


def _field_line(meta: dict) -> str:
    many = "one or more values" if meta["type"] == "multiselect" else "exactly one value"
    options = ", ".join(f"{option['value']} ({option.get('label') or option['value']})" for option in meta["options"])
    return f'- "{meta["key"]}": {extras.label(meta, meta["key"])}; {many} of: {options}'


def _answer_schema(fields: list[dict]) -> dict:
    properties = {meta["key"]: _property(meta) for meta in fields}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _property(meta: dict) -> dict:
    enum = {"type": "string", "enum": [option["value"] for option in meta["options"]]}
    if meta["type"] == "multiselect":
        return {"type": "array", "items": enum, "minItems": 1, "uniqueItems": True}
    return enum


def _draft(adapter: Any, channel: str, meta: dict, answer: Any) -> ProposalDraft:
    locator = extras.locator(adapter, field_key=meta["key"], channel=channel, language=None)
    value = {"options": list(answer)} if meta["type"] == "multiselect" else {"option": answer}
    return ProposalDraft(target_kind=TARGET_KIND, target_locator=locator, proposed_value=value)
