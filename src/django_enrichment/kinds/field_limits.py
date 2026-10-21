# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Length range of a short text field — option `field_limits` of kind `write` (a leaf: no Django, no services).

A model does not count characters, so a prose rule ("140-160 characters") is not enough. The range is a number
pair per field key: the prompt and the answer schema show it to the model, kind `write` rewrites a short value
outside it once, and QA flags what is still outside (`LIMIT`, `qa_service`). Only a short field (`max` up to
`CORRECTABLE_MAX`) is rewritten: a longer text is the operator's to edit, and a second call would rewrite it whole.
"""

from pydantic import BaseModel, Field, TypeAdapter, model_validator

CORRECTABLE_MAX = 300


class FieldLimit(BaseModel):
    min: int = Field(0, ge=0, description="Fewest characters; 0 = no minimum", examples=[120])
    max: int = Field(ge=1, description="Most characters", examples=[160])

    @model_validator(mode="after")
    def _ordered(self) -> "FieldLimit":
        if self.min > self.max:
            raise ValueError("min must not exceed max")
        return self

    @property
    def correctable(self) -> bool:
        return self.max <= CORRECTABLE_MAX

    def covers(self, text: str) -> bool:
        return self.min <= len(text) <= self.max

    def phrase(self) -> str:
        """The range as prompt text."""
        if self.min == 0:
            return f"at most {self.max} characters"
        return f"between {self.min} and {self.max} characters, spaces included"

    def gap(self, text: str) -> str:
        """How far `text` is from the range: `195 characters, 35 too long`."""
        length = len(text)
        if length > self.max:
            return f"{length} characters, {length - self.max} too long"
        return f"{length} characters, {self.min - length} too short"

    def json_schema(self) -> dict:
        """`minLength` / `maxLength` of the range (a zero minimum is left out)."""
        return {"maxLength": self.max, **({"minLength": self.min} if self.min else {})}


_LIMITS = TypeAdapter(dict[str, FieldLimit])


def configured(options: dict | None) -> dict[str, FieldLimit]:
    """The ranges of a worker's `options.field_limits`, by field key (none configured → empty)."""
    return _LIMITS.validate_python((options or {}).get("field_limits") or {})
