# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `sections` — a long text written section by section from the approved outline, as an artifact.

The outline (the `{"text"}` input of `outline_step`) is cut at its numbered headings (`### N. <title>` plus the
lines under it). One completion per section, in order: it sees its own outline block and every section written
before it, so it continues the thought instead of repeating it. Then one completion writes the lead and the closing
from the finished sections. Value `{"text"}` = lead, sections, closing; every section starts with `## <its outline
title>` whatever heading the model wrote. What the lead, the sections and the closing should be like is the worker's
system prompt (data), the kind only orchestrates. A regeneration hands each section only its own part of the
previous version (matched by title) and the operator instruction, before the section's task; the lead and closing
call sees the whole previous version. An outline without numbered headings is written in one call (logged). Cost =
the sum of the calls (one unknown cost makes it unknown); the calls are tagged `enrichment.sections` and keep the
unit's lease alive (`toolbox_calls` → `lease_keepalive`).
"""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import Field

from django_enrichment.kinds.base import Kind, ProposalDraft, fenced, with_cost
from django_enrichment.kinds.generate import GenerateOptions
from django_enrichment.kinds.step import StepParams, build_prompt, locator
from django_enrichment.services import toolbox_calls

TARGET_KIND = "artifact"
_HEADING = re.compile(r"^###\s+(\d+)\.\s*(.+?)\s*$", re.MULTILINE)
_BODY_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_LEADING_HEADINGS = re.compile(r"\A(?:\s*#{1,2}\s[^\n]*\n?)+")
_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
_ENDS_SCHEMA = {
    "type": "object",
    "properties": {"lead": {"type": "string"}, "closing": {"type": "string"}},
    "required": ["lead", "closing"],
    "additionalProperties": False,
}
_ENDS_TASK = (
    "The sections of the article are written: the article_body block. Write its lead and its closing as your "
    "instructions describe them, and nothing else: `lead` is the opening before the first section, `closing` the "
    "closing reflection after the last one. No new sections, no recap of the sections."
)
_WHOLE_TASK = (
    "Write the whole article body in this one call: the lead, every section of the outline and the closing. Answer "
    "with it as markdown in the `text` property."
)
_logger = logging.getLogger("process")


@dataclass(frozen=True)
class OutlineSection:
    number: int
    title: str
    block: str


@dataclass
class _Spend:
    costs: list[Decimal | None] = field(default_factory=list)

    def total(self) -> Decimal | None:
        return None if None in self.costs else sum(self.costs, Decimal(0))


class SectionsParams(StepParams):
    outline_step: str = Field("outline", description="Step whose markdown outline is written", examples=["outline"])


class SectionsKind(Kind):
    Options = GenerateOptions
    Params = SectionsParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:  # noqa: ARG002
        params = SectionsParams.model_validate(task.params)
        params.rules = [*GenerateOptions.model_validate(worker.options).rules, *params.rules]
        outline = str((params.inputs.get(params.outline_step) or {}).get("text") or "")
        sections, spend = outline_sections(outline), _Spend()
        if not sections:
            _logger.warning("sections: outline of task %s has no numbered `### N.` headings, one call", task.pk)
        writer = _Writer(worker, task, params, spend)
        text = writer.by_sections(sections) if sections else writer.call([_WHOLE_TASK], _TEXT_SCHEMA)["text"]
        draft = ProposalDraft(target_kind=TARGET_KIND, target_locator=locator(params), proposed_value={"text": text})
        return with_cost([draft], spend.total())


def outline_sections(outline: str) -> list[OutlineSection]:
    """The numbered `### N. <title>` headings of an outline, each with the lines under it."""
    return [OutlineSection(int(m.group(1)), m.group(2), block) for m, block in _blocks(_HEADING, outline)]


def body_sections(text: str) -> dict[str, str]:
    """The `## <title>` sections of an article body by lowercased title (what a regenerated section is shown)."""
    return {match.group(1).strip().lower(): block for match, block in _blocks(_BODY_HEADING, text)}


def _blocks(heading: re.Pattern, text: str) -> list[tuple[re.Match, str]]:
    if not (matches := list(heading.finditer(text))):
        return []
    ends = [match.start() for match in matches[1:]] + [len(text)]
    return [(match, text[match.start() : end].strip()) for match, end in zip(matches, ends, strict=True)]


def with_heading(text: str, title: str) -> str:
    """`text` under `## <title>`: an H1 or H2 the model put first (another wording, a number) is replaced."""
    return f"## {title}\n\n{_LEADING_HEADINGS.sub('', text.strip()).strip()}"


@dataclass
class _Writer:
    worker: Any
    task: Any
    params: SectionsParams
    spend: _Spend

    def by_sections(self, sections: list[OutlineSection]) -> str:
        written: list[str] = []
        for section in sections:
            written.append(self.section(section, len(sections), written))
        body = "\n\n".join(written)
        previous = self.previous_text()
        ends = self.call([fenced("article_body", body), *self.revision(previous), _ENDS_TASK], _ENDS_SCHEMA)
        return "\n\n".join(part.strip() for part in (ends.get("lead", ""), body, ends.get("closing", "")) if part)

    def section(self, section: OutlineSection, total: int, written: list[str]) -> str:
        so_far = fenced("sections_so_far", "\n\n".join(written)) if written else ""
        previous = body_sections(self.previous_text()).get(section.title.strip().lower(), "")
        task = (
            f"Write section {section.number} of {total} of the article body, and only that section: its outline "
            f"block is the section_outline block. Start with `## {section.title}`. Continue the thought of the "
            "sections so far; never repeat a fact, example or metaphor they used, and do not copy their skeleton or "
            "openers. No lead, no closing, no H1 title, no FAQ. Answer with the section as markdown in the `text` "
            "property."
        )
        parts = [fenced("section_outline", section.block), so_far, *self.revision(previous), task]
        return with_heading(str(self.call(parts, _TEXT_SCHEMA)["text"]), section.title)

    def previous_text(self) -> str:
        return str((self.params.previous or {}).get("text") or "")

    def revision(self, previous: str) -> list[str]:
        """A regeneration, scoped to the part being written: its previous version and the operator instruction."""
        if self.params.previous is None and not self.params.instruction:
            return []
        instruction = self.params.instruction or "write a better one than the previous version"
        note = f"This is a new version of the part you write. Operator instruction: {instruction}"
        return [fenced("previous_version", previous) if previous else "", note]

    def call(self, parts: list[str], schema: dict) -> dict:
        # The regeneration goes in `parts` (scoped to this call), never at the end of the prompt.
        unscoped = self.params.model_copy(update={"previous": None, "instruction": ""})
        prompt = build_prompt(self.worker, self.task, unscoped, kind="sections", parts=parts)
        answer = toolbox_calls.complete(
            system=prompt.system,
            user=prompt.user,
            json_schema=schema,
            model=self.worker.model,
            kind="sections",
            task=self.task,
        )
        self.spend.costs.append(answer.cost)
        return answer.value
