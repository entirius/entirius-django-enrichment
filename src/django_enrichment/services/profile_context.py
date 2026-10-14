# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The company profile as prompt context (contract A4 § 2): `render(data, task_type, kind) -> (instructions, data)`.

Sections come from `routing` (the task type, then the kind, then `DEFAULT_ROUTING`), ordered facts → brand →
the rest (conflict priority). The operator-authored text is the instruction channel (`always_rules` always
first); the facts registry is rendered as a data table. Both together are capped at `CAP_CHARS`; facts keep
priority and a cut is reported in the instructions.
"""

import logging

from django_enrichment.kinds.base import join_prompts

_logger = logging.getLogger("process")

CAP_CHARS = 12_000
DEFAULT_ROUTING: dict[str, list[str]] = {
    "write": ["brand", "persona", "tone", "language", "facts"],
    "pick": ["brand", "language"],
    "generate": ["brand", "persona", "tone", "language", "facts", "proof", "seo"],
    "questions": ["brand", "facts"],
}
FALLBACK_SECTIONS = ["brand"]
PRIORITY = ["facts", "brand"]
CONFLICT_NOTE = "Company profile. When its parts conflict, the facts win over the brand, the brand over the rest."
_TITLES = {
    "brand": "Brand",
    "persona": "Audience",
    "tone": "Tone of voice",
    "language": "Language",
    "headlines": "Headlines",
    "proof": "Proof",
    "seo": "SEO",
    "checklists": "Checklists",
}
_LISTS = {
    "brand": [("differentiators", "Differentiators"), ("never_say", "Never say")],
    "tone": [("ai_patterns", "Avoid")],
}
_FACT_COLUMNS = ["id", "label", "value", "source", "verified_at"]


def render(data: dict, *, task_type: str, kind: str) -> tuple[str, str]:
    """`(instructions, facts table)`, both blank when the routed sections are empty."""
    sections = routed_sections(data, task_type=task_type, kind=kind)
    texts = [_section(name, data.get(name) or {}) for name in sections if name != "facts"]
    instructions = join_prompts(_bullets("Always", data.get("always_rules", [])), *texts)
    facts = facts_table(data.get("facts", [])) if "facts" in sections else ""
    if instructions or facts:
        instructions = join_prompts(CONFLICT_NOTE, instructions)
    return capped(instructions, facts)


def routed_sections(data: dict, *, task_type: str, kind: str) -> list[str]:
    """Routed section names, facts first, then brand, then the rest in routing order."""
    routing = data.get("routing", {})
    names = routing.get(task_type) or routing.get(kind) or DEFAULT_ROUTING.get(kind, FALLBACK_SECTIONS)
    return [name for name in PRIORITY if name in names] + [name for name in names if name not in PRIORITY]


def _section(name: str, value: dict) -> str:
    parts = [value.get("text", "").strip()]
    parts += [_bullets(title, value.get(key, [])) for key, title in _LISTS.get(name, [])]
    if name == "persona":
        parts += [join_prompts(f"### {item['name']}", item.get("text", "")) for item in value.get("personas", [])]
    if name == "language":
        parts += [_spellings(value.get("spellings", [])), _glossary(value.get("glossary", []))]
    body = join_prompts(*parts)
    return f"## {_TITLES.get(name, name)}\n{body}" if body else ""


def _bullets(title: str, items: list[str]) -> str:
    return f"{title}:\n" + "\n".join(f"- {item}" for item in items) if items else ""


def _spellings(spellings: list[dict]) -> str:
    return _bullets("Spelling", [f"write {item['right']!r}, not {item['wrong']!r}" for item in spellings])


def _glossary(glossary: list[dict]) -> str:
    lines = [_term(item) for item in glossary]
    return _bullets("Glossary", lines)


def _term(item: dict) -> str:
    translations = ", ".join(f"{lang}: {text}" for lang, text in (item.get("translations") or {}).items())
    return f"{item['term']} ({translations})" if translations else item["term"]


def facts_table(facts: list[dict]) -> str:
    """The facts registry as a markdown table (the only numbers and claims the text may use)."""
    if not facts:
        return ""
    rows = [_FACT_COLUMNS, ["---"] * len(_FACT_COLUMNS)]
    rows += [[_cell(fact.get(column, "")) for column in _FACT_COLUMNS] for fact in facts]
    return "\n".join(f"| {' | '.join(row)} |" for row in rows)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def capped(instructions: str, facts: str) -> tuple[str, str]:
    """Cut both to `CAP_CHARS` in total — facts first — and say so in the instructions."""
    total = len(instructions) + len(facts)
    if total <= CAP_CHARS:
        return instructions, facts
    facts = facts[:CAP_CHARS]
    instructions = instructions[: CAP_CHARS - len(facts)]
    shown = len(instructions) + len(facts)
    _logger.warning("company profile truncated: %s of %s characters", shown, total)
    return join_prompts(instructions, f"[Company profile truncated: {shown} of {total} characters shown.]"), facts
