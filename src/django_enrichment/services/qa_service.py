# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Deterministic QA checks of a text value (contract A4 § 4) — `warnings` `[{code, message, span}]`.

- `rule`: the active profile `rules` (regex + flags) and `language.spellings` (wrong → right). A pattern
  that does not compile disables only its own rule (logged), never the check.
- `NUMBER_NOT_IN_FACTS`: a number that is neither in the profile facts registry nor in the task's source
  context — its params (prompt, brief, inputs) and the context numbers the kinds stored per subject
  (`params.context_numbers`, see `record_context`).
- `FACT_NOT_IN_PROFILE`: a citation `(profile fact: <id>)` whose `<id>` is not an `id` of the merged profile's
  facts registry, once per distinct id. The citation format is a contract with the workflow prompts that ask the
  model to label claims with their source (the id column of the facts table, `profile_context`): a prompt that
  changes it must change `FACT_CITATION` with it. Case and the whitespace around the colon and the id are free.

A `{"data"}` value (a JSON step: metadata, search phrases) gets `LIMIT` warnings against the step's
`params.json_schema` (`maxLength` / `minLength` of strings, `maxItems` of arrays — the provider only sees them as
hints, so they are enforced here), then the rule, number and citation checks on each of its text fields; its
warnings carry the field's `path` (`faq[2].answer`) and a span inside that field.

A `{"text"}` value gets a `LIMIT` warning (no `path`) when its field has a character range in the `options.field_limits`
of the `write` worker that produced it (`kinds.field_limits`) and the text is outside it.

`revision_service` runs `check` for every new revision; `recheck` re-runs it on demand. Other values (a select,
media) have no warnings.
"""

import json
import logging
import re
from collections.abc import Iterator

from django.db import transaction
from django.utils import timezone

from django_enrichment.adapters import get_adapter
from django_enrichment.enums import REVISABLE_STATUSES
from django_enrichment.kinds import extras
from django_enrichment.kinds.field_limits import FieldLimit, configured
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import profile_service, proposal_scope, qa_numbers, worker_service
from django_enrichment.services.revise_values import RevisionNotApplicableError

_logger = logging.getLogger("process")

RULE = "rule"
NUMBER_NOT_IN_FACTS = "NUMBER_NOT_IN_FACTS"
FACT_NOT_IN_PROFILE = "FACT_NOT_IN_PROFILE"
LIMIT = "LIMIT"
CHECKLIST = "checklist"
CONTEXT_NUMBERS = "context_numbers"
SOURCE_PARAMS = ("prompt", "brief", "inputs")
MAX_WARNINGS = 50
_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}
FACT_CITATION = re.compile(r"\(\s*profile\s+fact\s*:[^\S\n]*([^)\s][^)\n]*?)[^\S\n]*\)", re.IGNORECASE)


def text_of(value: dict) -> str | None:
    """The checked text of a value, None for values without one (select, media, data)."""
    return value.get("text") if isinstance(value.get("text"), str) else None


def check(proposal: ContentProposal, value: dict) -> list[dict]:
    """The warnings of `value` as a value of `proposal` (its channel's profile, its task's context)."""
    if (text := text_of(value)) is None and not isinstance(value.get("data"), dict):
        return []
    profile = profile_service.merged(proposal_scope.channel_of(proposal))
    allowed = allowed_numbers(proposal, profile)
    if text is not None:
        return [*field_limit_warnings(proposal, text), *text_warnings(text, profile, allowed)][:MAX_WARNINGS]
    return data_warnings(value["data"], _schema_of(proposal), profile, allowed)[:MAX_WARNINGS]


def data_warnings(data: dict, schema: dict, profile: dict, allowed: set[str]) -> list[dict]:
    """The schema limits of a data value, then the text checks of each of its text fields (with their path)."""
    warnings = list(limit_warnings(data, schema))
    for path, text in text_fields(data):
        warnings += [{**warning, "path": path} for warning in text_warnings(text, profile, allowed)]
    return warnings


def text_warnings(text: str, profile: dict, allowed: set[str]) -> Iterator[dict]:
    """The rule, number and fact-citation checks of one text."""
    yield from rule_warnings(text, profile)
    yield from number_warnings(text, allowed)
    yield from fact_citation_warnings(text, profile)


def field_limit_warnings(proposal: ContentProposal, text: str) -> Iterator[dict]:
    """`LIMIT` when `text` is outside the character range its field has in the producing worker's options."""
    if (limit := _field_limit(proposal)) is None:
        return
    for warning in _length_warnings(text, limit.json_schema(), ""):
        yield {key: value for key, value in warning.items() if key != "path"}


def _field_limit(proposal: ContentProposal) -> FieldLimit | None:
    worker = worker_service.from_source(proposal.source)
    if worker is None or not (limits := configured(worker.options)):
        return None
    key = extras.field_key(get_adapter(proposal.target_module), proposal.target_locator or {})
    return limits.get(key) if key else None


def limit_warnings(value: object, schema: dict, path: str = "") -> Iterator[dict]:
    """`maxLength` / `minLength` of strings and `maxItems` of arrays, walked through `properties` and `items`."""
    if isinstance(value, str):
        yield from _length_warnings(value, schema, path)
    elif isinstance(value, list):
        if (limit := schema.get("maxItems")) is not None and len(value) > limit:
            yield _limit(path, f"{len(value)} items, the limit is {limit}.")
        for index, item in enumerate(value):
            yield from limit_warnings(item, schema.get("items") or {}, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                yield from limit_warnings(value[key], sub, f"{path}.{key}" if path else key)


def _length_warnings(text: str, schema: dict, path: str) -> Iterator[dict]:
    if (limit := schema.get("maxLength")) is not None and len(text) > limit:
        yield _limit(path, f"{len(text)} characters, the limit is {limit}.")
    if (least := schema.get("minLength")) is not None and len(text) < least:
        yield _limit(path, f"{len(text)} characters, at least {least}.")


def _limit(path: str, message: str) -> dict:
    return {"code": LIMIT, "message": message, "span": None, "path": path}


def text_fields(value: object, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string of a data value with its path (`faq[0].answer`)."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from text_fields(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from text_fields(item, f"{path}.{key}" if path else key)


def _schema_of(proposal: ContentProposal) -> dict:
    params = (proposal.task.params if proposal.task else None) or {}
    return params.get("json_schema") if isinstance(params.get("json_schema"), dict) else {}


def recheck(proposal: ContentProposal) -> ContentProposal:
    """Recompute the current value's warnings onto the proposal and its latest revision.
    Raises `ValueError` (not revisable) or `RevisionNotApplicableError` (value changed meanwhile)."""
    value = proposal.proposed_value
    return store(proposal, check(proposal, value), base_value=value)


def store(
    proposal: ContentProposal, warnings: list[dict], *, base_value: dict, checked: bool = False
) -> ContentProposal:
    """Save `warnings` (computed on `base_value`) on the proposal and its latest revision (they always agree).
    `checked` = they include the checklist check (`standard_checked_at` = now); otherwise it is cleared.

    Row-locked like `revision_service.add_revision`: raises `ValueError` unless the proposal is still
    revisable, `RevisionNotApplicableError` when its value is no longer `base_value`."""
    with transaction.atomic():
        locked = ContentProposal.objects.select_for_update().get(pk=proposal.pk)
        if locked.status not in REVISABLE_STATUSES:
            raise ValueError(f"only pending or drifted proposals can be checked (status: {locked.status})")
        if locked.proposed_value != base_value:
            raise RevisionNotApplicableError("the value changed while the check was running")
        locked.warnings = warnings
        locked.standard_checked_at = timezone.now() if checked else None
        locked.save(update_fields=["warnings", "standard_checked_at", "modified_at"])
        if latest := locked.revisions.order_by("-number").first():
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


def fact_citation_warnings(text: str, profile: dict) -> Iterator[dict]:
    """One warning per distinct `(profile fact: <id>)` whose id is not in the profile's facts registry (quotes or
    backticks around the id, as markdown output often has them, are not part of it)."""
    known = {fact.get("id") for fact in profile.get("facts", [])}
    seen: set[str] = set()
    for match in FACT_CITATION.finditer(text):
        cited = match.group(1).strip().strip("`'\"").strip()
        if not cited or cited in known or cited in seen:
            continue
        seen.add(cited)
        span = _span(match.start(), match.end(), match.group())
        yield {"code": FACT_NOT_IN_PROFILE, "message": _fact_message(cited, empty=not known), "span": span}


def _fact_message(cited: str, *, empty: bool) -> str:
    if empty:
        return f'The company profile has no facts; "{cited}" cannot be cited from it.'
    return f'The profile fact "{cited}" is not in the company profile\'s facts registry.'


def allowed_numbers(proposal: ContentProposal, profile: dict) -> set[str]:
    """Numbers of the facts registry, the task's source params (`SOURCE_PARAMS`, never its configuration)
    and the subject's stored context numbers."""
    facts = " ".join(
        f"{fact.get('id', '')} {fact.get('label', '')} {fact.get('value', '')}" for fact in profile.get("facts", [])
    )
    params = (proposal.task.params if proposal.task else None) or {}
    source = json.dumps([params.get(key) for key in SOURCE_PARAMS], ensure_ascii=False)
    stored = params.get(CONTEXT_NUMBERS, {}).get(proposal.subject_ref, [])
    return qa_numbers.values_in(facts) | qa_numbers.values_in(source) | set(stored)


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
