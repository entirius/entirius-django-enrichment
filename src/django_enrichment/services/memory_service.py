# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Memory (contract A4 § 3): what the studio remembers between tasks, and how prompts recall it.

`recall` answers a data block — the subject's entries, then its category's (adapter extra `category_of`),
newest first, only active and unexpired ones, cut to a character budget. Writers: `record_answers` (approved
workflow questions), `suggest_rule` (rule promotion target `"profile"`), `suggest_fact` (a number QA flagged,
vouched for by the operator) and `remember` for the rest
(augmentations, which look up their own earlier result with `fresh`). Promotion into the profile lives in `memory_promotion`.
"""

import json
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from django_enrichment.adapters import get_adapter
from django_enrichment.enums import MemoryKind, MemoryStatus
from django_enrichment.models import ContentProposal, MemoryEntry, WorkflowRun
from django_enrichment.services import proposal_scope

MAX_TITLE = 200
MAX_TEXT = 4000
RECALL_LIMIT = 50
EDITABLE_STATUSES = frozenset({MemoryStatus.ACTIVE.value, MemoryStatus.DISMISSED.value})


def remember(entry: MemoryEntry) -> MemoryEntry:
    """Save a new entry, title and text cut to their limits. Raises `ValueError` for an unknown kind."""
    if entry.kind not in MemoryKind.values:
        raise ValueError(f"unknown memory kind {entry.kind!r}")
    entry.title, entry.text = entry.title[:MAX_TITLE], entry.text[:MAX_TEXT]
    entry.save()
    return entry


def recall(*, channel: str, subject_module: str, subject_ref: str, kinds: list[str], budget_chars: int) -> str:
    """The remembered entries as one line each, subject first, then its category; blank when none."""
    refs = _subject_then_category(channel, subject_module, subject_ref)
    lines = [_line(entry) for ref in refs for entry in _active(channel, subject_module, ref, kinds)]
    return _budgeted(lines, budget_chars)


def fresh(*, channel: str, subject_module: str, subject_ref: str, kind: str, language: str = "") -> MemoryEntry | None:
    """The newest active, unexpired entry of `kind` in `language` (`data.language`, blank = none recorded) about
    the subject, else about its category."""
    for ref in _subject_then_category(channel, subject_module, subject_ref):
        entries = _active(channel, subject_module, ref, [kind])
        if entry := next((e for e in entries if (e.data or {}).get("language", "") == language), None):
            return entry
    return None


def _subject_then_category(channel: str, subject_module: str, subject_ref: str) -> list[str]:
    refs = [subject_ref]
    if (category := category_of(subject_module, subject_ref, channel=channel)) and category != subject_ref:
        refs.append(category)
    return refs


def category_of(subject_module: str, subject_ref: str, *, channel: str = "") -> str | None:
    """The subject's category ref in `channel` via the adapter extra `category_of`; None without one."""
    if not subject_module or not subject_ref:
        return None
    try:
        adapter = get_adapter(subject_module)
    except ValueError:
        return None
    category = getattr(adapter, "category_of", None)
    return category(subject_ref, channel=channel) if category else None


def _active(channel: str, subject_module: str, subject_ref: str, kinds: list[str]) -> QuerySet[MemoryEntry]:
    unexpired = Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
    entries = MemoryEntry.objects.filter(
        unexpired,
        channel__in={"", channel},
        subject_module=subject_module,
        subject_ref=subject_ref,
        kind__in=kinds,
        status=MemoryStatus.ACTIVE.value,
    )
    return entries.order_by("-created_at", "-pk")[:RECALL_LIMIT]


def _line(entry: MemoryEntry) -> str:
    body = entry.text or (json.dumps(entry.data, ensure_ascii=False) if entry.data else "")
    return f"- [{entry.kind}] {entry.title}: {body}".replace("\n", " ")


def _budgeted(lines: list[str], budget: int) -> str:
    """Whole lines while they fit; a first line longer than the budget is cut."""
    kept: list[str] = []
    for line in lines:
        room = budget - sum(len(item) + 1 for item in kept)
        if len(line) > room:
            if not kept:
                kept.append(line[: max(room, 0)])
            break
        kept.append(line)
    return "\n".join(kept)


def record_answers(*, run: WorkflowRun, step: str, value: dict) -> None:
    """One `answer` entry per answered question of an approved questions step (general, the run's channel)."""
    answers = value.get("answers") or {}
    for question in value.get("questions", []):
        if answer := str(answers.get(question.get("id", "")) or "").strip():
            remember(_answer(run, step, question, answer))


def _answer(run: WorkflowRun, step: str, question: dict, answer: str) -> MemoryEntry:
    return MemoryEntry(
        channel=run.channel,
        kind=MemoryKind.ANSWER.value,
        title=str(question.get("question", "")),
        text=answer,
        data={"question_id": question.get("id", ""), "run": run.uid, "step": step},
        source=f"run:{run.uid}/{step}",
        created_by=run.created_by,
    )


def suggest_rule(proposal: ContentProposal, rule: str) -> MemoryEntry:
    """A `rule_suggestion` for the profile, from a correction rule of `proposal` (general, its channel)."""
    entry = MemoryEntry(
        channel=proposal_scope.channel_of(proposal),
        kind=MemoryKind.RULE_SUGGESTION.value,
        title=rule,
        text=rule,
        source=f"proposal:{proposal.pk}",
    )
    return remember(entry)


def suggest_fact(proposal: ContentProposal, *, value: str, label: str = "", user: Any = None) -> MemoryEntry:
    """A `fact_suggestion` for the profile facts registry, from a number QA flagged in `proposal`."""
    entry = MemoryEntry(
        channel=proposal_scope.channel_of(proposal),
        kind=MemoryKind.FACT_SUGGESTION.value,
        title=label or value,
        text=value,
        data={"label": label or value, "value": value, "source": f"proposal:{proposal.pk}"},
        source=f"proposal:{proposal.pk}",
        created_by=user,
    )
    return remember(entry)


def rule_suggestions(channel: str) -> list[str]:
    """Texts of the channel's active rule suggestions, oldest first."""
    entries = list_entries(channel=channel, kind=MemoryKind.RULE_SUGGESTION.value, status=MemoryStatus.ACTIVE.value)
    return list(entries.order_by("created_at", "pk").values_list("text", flat=True))


def list_entries(
    *, channel: str | None = None, kind: str | None = None, subject_ref: str | None = None, status: str | None = None
) -> QuerySet[MemoryEntry]:
    filters = {"channel": channel, "kind": kind, "subject_ref": subject_ref, "status": status}
    return MemoryEntry.objects.filter(**{key: value for key, value in filters.items() if value is not None})


def get_entry(entry_id: int) -> MemoryEntry:
    if (entry := MemoryEntry.objects.filter(pk=entry_id).first()) is None:
        raise ValueError(f"memory entry {entry_id} not found")
    return entry


def set_status(entry: MemoryEntry, status: str) -> MemoryEntry:
    """Dismiss or re-activate; `promoted` is reached only through promotion. Raises `ValueError`."""
    if status not in EDITABLE_STATUSES:
        raise ValueError(f"status must be one of {sorted(EDITABLE_STATUSES)}")
    if entry.status == MemoryStatus.PROMOTED.value:
        raise ValueError("a promoted entry cannot change its status")
    entry.status = status
    entry.save(update_fields=["status", "modified_at"])
    return entry


def delete_entry(entry: MemoryEntry) -> None:
    entry.delete()
