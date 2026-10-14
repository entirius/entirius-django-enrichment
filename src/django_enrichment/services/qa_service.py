# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Deterministic QA checks of a text value (contract A4 § 4) — `warnings` `[{code, message, span}]`.

- `rule`: the active profile `rules` (regex + flags) and `language.spellings` (wrong → right). A pattern
  that does not compile disables only its own rule (logged), never the check.
- `NUMBER_NOT_IN_FACTS`: a number that is neither in the profile facts registry nor in the task's source
  context — its params (prompt, brief, inputs) and the context numbers the kinds stored per subject
  (`params.context_numbers`, see `record_context`).

`revision_service` runs `check` for every new revision; `recheck` re-runs it on demand. Only `{"text"}`
values are checked; other values have no warnings.
"""

import json
import logging
import re
from collections.abc import Iterator

from django.db import transaction

from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import profile_service, proposal_scope, qa_numbers

_logger = logging.getLogger("process")

RULE = "rule"
NUMBER_NOT_IN_FACTS = "NUMBER_NOT_IN_FACTS"
CHECKLIST = "checklist"
CONTEXT_NUMBERS = "context_numbers"
MAX_WARNINGS = 50
_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


def text_of(value: dict) -> str | None:
    """The checked text of a value, None for values without one (select, media, data)."""
    return value.get("text") if isinstance(value.get("text"), str) else None


def check(proposal: ContentProposal, value: dict) -> list[dict]:
    """The warnings of `value` as a value of `proposal` (its channel's profile, its task's context)."""
    if not (text := text_of(value)):
        return []
    profile = profile_service.merged(proposal_scope.channel_of(proposal))
    warnings = [*rule_warnings(text, profile), *number_warnings(text, allowed_numbers(proposal, profile))]
    return warnings[:MAX_WARNINGS]


def recheck(proposal: ContentProposal) -> ContentProposal:
    """Recompute the current value's warnings onto the proposal and its latest revision."""
    return store(proposal, check(proposal, proposal.proposed_value))


def store(proposal: ContentProposal, warnings: list[dict]) -> ContentProposal:
    """Save `warnings` on the proposal and its latest revision (they always agree)."""
    with transaction.atomic():
        ContentProposal.objects.filter(pk=proposal.pk).update(warnings=warnings)
        if latest := proposal.revisions.order_by("-number").first():
            latest.warnings = warnings
            latest.save(update_fields=["warnings", "modified_at"])
    proposal.refresh_from_db()
    return proposal


def rule_warnings(text: str, profile: dict) -> Iterator[dict]:
    """Matches of the active lint rules, then of the spellings."""
    for rule in profile.get("rules", []):
        if rule.get("active", True) and (pattern := _compiled(rule)):
            yield from _matches(text, pattern, _rule_message(rule))
    for spelling in (profile.get("language") or {}).get("spellings", []):
        pattern = re.compile(rf"(?<!\w){re.escape(spelling['wrong'])}(?!\w)", re.IGNORECASE)
        yield from _matches(text, pattern, f"Write {spelling['right']!r}, not {spelling['wrong']!r}.")


def _compiled(rule: dict) -> re.Pattern | None:
    flags = 0
    for flag in rule.get("flags", ""):
        flags |= _FLAGS.get(flag, 0)
    try:
        return re.compile(rule["pattern"], flags)
    except re.error as exc:
        _logger.warning("profile rule %r disabled: its pattern does not compile (%s)", rule.get("id"), exc)
        return None


def _rule_message(rule: dict) -> str:
    return " ".join(part for part in (rule["message"], f"Do: {rule['do']}" if rule.get("do") else "") if part)


def _matches(text: str, pattern: re.Pattern, message: str) -> Iterator[dict]:
    for match in pattern.finditer(text):
        if match.end() > match.start():
            yield {"code": RULE, "message": message, "span": _span(match.start(), match.end(), match.group())}


def number_warnings(text: str, allowed: set[str]) -> Iterator[dict]:
    """One warning per distinct number of `text` that matches nothing in `allowed`."""
    seen: set[str] = set()
    for token in qa_numbers.tokens(text):
        if token.values & allowed or token.text in seen:
            continue
        seen.add(token.text)
        message = f"The number {token.text} is not in the facts registry or the source context."
        yield {"code": NUMBER_NOT_IN_FACTS, "message": message, "span": _span(token.start, token.end, token.text)}


def allowed_numbers(proposal: ContentProposal, profile: dict) -> set[str]:
    """Numbers of the facts registry, the task params and the subject's stored context numbers."""
    facts = " ".join(f"{fact.get('label', '')} {fact.get('value', '')}" for fact in profile.get("facts", []))
    params = dict((proposal.task.params if proposal.task else None) or {})
    stored = params.pop(CONTEXT_NUMBERS, {}).get(proposal.subject_ref, [])
    return qa_numbers.values_in(facts) | qa_numbers.values_in(json.dumps(params, ensure_ascii=False)) | set(stored)


def _span(start: int, end: int, text: str) -> dict:
    return {"start": start, "end": end, "text": text}


def record_context(task: EnrichmentTask | None, subject_ref: str, context: object) -> None:
    """Store the numbers of a subject's source context on the task params (`params.context_numbers`)."""
    if getattr(task, "pk", None) is None:
        return
    numbers = qa_numbers.values_in(context if isinstance(context, str) else json.dumps(context, ensure_ascii=False))
    with transaction.atomic():
        locked = EnrichmentTask.objects.select_for_update().get(pk=task.pk)
        stored = dict(locked.params.get(CONTEXT_NUMBERS, {}))
        stored[subject_ref] = sorted(numbers | set(stored.get(subject_ref, [])))
        locked.params = {**locked.params, CONTEXT_NUMBERS: stored}
        locked.save(update_fields=["params", "modified_at"])
    task.params = locked.params
