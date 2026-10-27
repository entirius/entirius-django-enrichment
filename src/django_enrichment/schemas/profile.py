# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The company profile's JSON Schema (`profile_schema.json`, contract A4 § 2) and its validation.

`validation_errors` returns every problem with its path (`brand.differentiators.0`), so the API can answer
400 with one detail per field. Rule patterns must also compile as regular expressions.
"""

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

PROFILE_SCHEMA: dict = json.loads((Path(__file__).parent / "profile_schema.json").read_text(encoding="utf-8"))
SCHEMA_VERSION: int = PROFILE_SCHEMA["x-schema-version"]
_VALIDATOR = Draft202012Validator(PROFILE_SCHEMA)


def validation_errors(data: object) -> list[dict]:
    """`[{"path", "message"}]` — empty when `data` is a valid profile."""
    errors = [
        {"path": _path(error.absolute_path), "message": error.message}
        for error in sorted(_VALIDATOR.iter_errors(data), key=lambda error: list(map(str, error.absolute_path)))
    ]
    return errors or _pattern_errors(data)


def _path(parts) -> str:
    return ".".join(str(part) for part in parts) or "data"


def _pattern_errors(data: dict) -> list[dict]:
    errors = []
    for index, rule in enumerate(data.get("rules", [])):
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            errors.append({"path": f"rules.{index}.pattern", "message": f"not a valid regular expression: {exc}"})
    return errors
