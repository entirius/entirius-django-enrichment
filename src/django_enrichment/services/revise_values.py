# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Value shapes of a correction: the answer schema per shape and how an answer becomes a new value.

| shape     | value                              | answer                              |
|-----------|------------------------------------|-------------------------------------|
| `edits`   | `{"text"}` with HTML or > 300 chars | `{"edits": [{old, new}], "rewrite"}` |
| `text`    | short `{"text"}`                   | `{"text"}` (+ `maxLength`)          |
| `option`  | `{"option"}`                       | enum of the catalog option values   |
| `options` | `{"options"}`                      | array of that enum                  |

Edits apply all-or-nothing: every `old` must occur exactly once in the text at the moment it is
applied, else `RevisionNotApplicableError` and nothing is saved. HTML is sanitised with the field
kind's allowlist. Framework-agnostic: plain dicts in and out.
"""

import re

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from django_enrichment.kinds.field import sanitize_html

EDITS, TEXT, OPTION, OPTIONS = "edits", "text", "option", "options"
LONG_TEXT_CHARS = 300
VARIANT_COUNT = 3
_HTML_TAG = re.compile(r"<[a-zA-Z][^>]*>")


class RevisionNotApplicableError(Exception):
    """The model's answer cannot be applied to the current value (→ 422 REVISION_NOT_APPLICABLE)."""


class _Edit(BaseModel):
    old: str
    new: str


class _EditsAnswer(BaseModel):
    edits: list[_Edit] = []
    rewrite: str | None = None


def value_shape(value: dict, meta: dict | None) -> str:
    """The shape of a revisable value. Raises `ValueError` for anything else."""
    if isinstance(value.get("text"), str):
        return EDITS if is_html(value["text"], meta) or len(value["text"]) > LONG_TEXT_CHARS else TEXT
    if "option" in value:
        return OPTION
    if isinstance(value.get("options"), list):
        return OPTIONS
    raise ValueError("this proposal's value cannot be corrected (expected text, option or options)")


def is_html(text: str, meta: dict | None) -> bool:
    return (meta or {}).get("type") == "html" or bool(_HTML_TAG.search(text))


def answer_schema(shape: str, meta: dict | None) -> dict:
    if shape == EDITS:
        return _object({"edits": _edits_array(), "rewrite": {"type": ["string", "null"]}})
    return _object({shape: _item_schema(shape, meta)})


def variants_schema(shape: str, meta: dict | None) -> dict:
    items = _item_schema(TEXT if shape == EDITS else shape, meta)
    return _object({"candidates": {"type": "array", "items": items, "maxItems": VARIANT_COUNT}})


def answer_hint(shape: str, *, variants: bool) -> str:
    if variants:
        return f'Answer as JSON {{"candidates": [...]}} with {VARIANT_COUNT} alternative complete values.'
    if shape == EDITS:
        return (
            'Answer as JSON {"edits": [{"old": "...", "new": "..."}], "rewrite": null}. Each "old" must be an '
            "exact fragment that occurs once in the current value. Only when the change touches most of the "
            'text, set "rewrite" to the complete new value and leave "edits" empty.'
        )
    return f'Answer as JSON {{"{shape}": ...}} with the complete new value.'


def apply_answer(shape: str, value: dict, answer: dict, meta: dict | None) -> dict:
    """The new value built from the model's answer. Raises `RevisionNotApplicableError`."""
    if shape == EDITS:
        return {"text": _clean(_apply_edits(value["text"], answer), value["text"], meta)}
    return _full_value(shape, answer.get(shape), meta, original=value)


def candidates(shape: str, answer: dict, meta: dict | None, *, original: dict) -> list[dict]:
    """The variant values of a `variants` answer (nothing is saved). Raises `RevisionNotApplicableError`."""
    raw = answer.get("candidates")
    if not isinstance(raw, list) or not raw:
        raise RevisionNotApplicableError("the answer carries no candidates")
    key = TEXT if shape == EDITS else shape
    return [_full_value(key, item, meta, original=original) for item in raw[:VARIANT_COUNT]]


def _full_value(key: str, item: object, meta: dict | None, *, original: dict) -> dict:
    if key == TEXT:
        if not isinstance(item, str):
            raise RevisionNotApplicableError("the answer carries no text")
        return {"text": _clean(item, original.get("text", ""), meta)}
    allowed = _option_values(meta)
    picked = item if key == OPTIONS else [item]
    if not isinstance(picked, list) or any(not isinstance(v, str) for v in picked):
        raise RevisionNotApplicableError("the answer carries no option")
    if allowed and any(v not in allowed for v in picked):
        raise RevisionNotApplicableError("the answer picked an option the field does not offer")
    return {key: item}


def _apply_edits(text: str, answer: dict) -> str:
    try:
        parsed = _EditsAnswer.model_validate(answer)
    except PydanticValidationError as exc:
        raise RevisionNotApplicableError("the answer is not a list of edits") from exc
    if parsed.rewrite:
        return parsed.rewrite
    if not parsed.edits:
        raise RevisionNotApplicableError("the answer carries no edits")
    for edit in parsed.edits:
        if not edit.old or text.count(edit.old) != 1:
            raise RevisionNotApplicableError("an edit does not match exactly one place in the text")
        text = text.replace(edit.old, edit.new, 1)
    return text


def _clean(new_text: str, old_text: str, meta: dict | None) -> str:
    return sanitize_html(new_text) if is_html(old_text, meta) or is_html(new_text, meta) else new_text


def _item_schema(shape: str, meta: dict | None) -> dict:
    if shape == TEXT:
        max_length = (meta or {}).get("max_length")
        return {"type": "string", "maxLength": max_length} if max_length else {"type": "string"}
    option = {"type": "string", "enum": values} if (values := _option_values(meta)) else {"type": "string"}
    return option if shape == OPTION else {"type": "array", "items": option}


def _option_values(meta: dict | None) -> list[str]:
    return [option["value"] for option in (meta or {}).get("options") or []]


def _edits_array() -> dict:
    edit = _object({"old": {"type": "string"}, "new": {"type": "string"}})
    return {"type": "array", "items": edit}


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
