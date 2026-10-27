# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Profile completeness (contract A4 § 2): the minimum each section needs before the studio relies on it."""

from collections.abc import Callable


def _brand(data: dict) -> bool:
    return len(data.get("brand", {}).get("differentiators", [])) >= 1


def _persona(data: dict) -> bool:
    persona = data.get("persona", {})
    return bool(persona.get("personas")) or bool(persona.get("text", "").strip())


def _facts(data: dict) -> bool:
    return sum(1 for fact in data.get("facts", []) if str(fact.get("verified_at", "")).strip()) >= 3


def _tone(data: dict) -> bool:
    tone = data.get("tone", {})
    lines = [line for line in tone.get("text", "").splitlines() if line.strip()]
    return len(lines) >= 3 or len(tone.get("ai_patterns", [])) >= 1


def _language(data: dict) -> bool:
    language = data.get("language", {})
    return bool(language.get("spellings")) or bool(language.get("text", "").strip())


MINIMUMS: list[tuple[str, str, Callable[[dict], bool]]] = [
    ("brand", "at least 1 differentiator", _brand),
    ("persona", "at least 1 persona", _persona),
    ("facts", "at least 3 verified facts", _facts),
    ("tone", "at least 3 lines or 1 AI pattern", _tone),
    ("language", "at least 1 spelling rule or a text", _language),
]


def sections(data: dict) -> list[dict]:
    """`[{"key", "complete", "minimum"}]` in a fixed order."""
    return [{"key": key, "complete": check(data), "minimum": minimum} for key, minimum, check in MINIMUMS]
