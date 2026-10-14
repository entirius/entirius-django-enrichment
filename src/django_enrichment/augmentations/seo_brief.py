# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Augmentation `seo_brief` (contract A4 § 5) — which phrases a text should target, from search data.

The brief is per language: `context["language"]` (the kinds set it) goes to every SEO call as `language_code`
(blank → the toolbox market defaults), is stored in the brief and must match for reuse.

1. Reuse: a fresh memory `seo_brief` in the language about the subject, then its category → that brief, cost 0.
2. Research: a completion proposes 3-5 seed phrases from the context → toolbox SEO `search-volume/` and
   `keyword-ideas/` (one call each) → `serp/` for the top `SERP_TOP` seeds by volume.
3. A completion evaluates intent first (SERP intent, winnability, gaps; volume breaks ties) →
   `{primary, secondary, intent, serp_notes, gaps, search_volume}`, remembered for `TTL` under the category when
   the scope is `category` (`params.seo_scope`; default for kind `write`, falls back to the subject without a
   category), else under the subject.

Every call charges the task (`toolbox_calls`); a toolbox failure or an answer that does not fit the boundary
models (`seo_answers`) fails the subject with `SEO_BRIEF_<code>`; other errors propagate to the runner.
"""

import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.utils import timezone
from django_utils.toolbox import ToolboxError
from pydantic import ValidationError

from django_enrichment.augmentations import AugmentResult
from django_enrichment.augmentations.seo_answers import (
    EvaluateAnswer,
    KeywordItem,
    SeedsAnswer,
    SeoItems,
    SerpData,
)
from django_enrichment.augmentations.seo_prompts import (
    DATA_NOTE,
    EVALUATE_SCHEMA,
    EVALUATE_SYSTEM,
    SEEDS_SCHEMA,
    SEEDS_SYSTEM,
)
from django_enrichment.enums import MemoryKind
from django_enrichment.kinds.base import SubjectError, fenced, join_prompts
from django_enrichment.models import MemoryEntry
from django_enrichment.services import memory_service, toolbox_calls, worker_service

TTL = timedelta(days=90)
SERP_TOP = 3
SEEDS_MAX = 5  # keyword-ideas/ takes at most 5 phrases
SERP_DEPTH = 10
IDEAS_KEPT = 20
ORGANIC_KEPT = 10
CATEGORY_SCOPE_KINDS = frozenset({"write"})
MEMORY_KIND = MemoryKind.SEO_BRIEF.value


@dataclass(frozen=True)
class _Subject:
    channel: str
    module: str
    ref: str
    language: str


def run(*, task: Any, subject_ref: str, context: dict) -> AugmentResult:
    subject = _subject(task, subject_ref, context)
    found = memory_service.fresh(
        channel=subject.channel,
        subject_module=subject.module,
        subject_ref=subject.ref,
        kind=MEMORY_KIND,
        language=subject.language,
    )
    if found:
        return AugmentResult(block=render(found.data), cost=Decimal(0))
    try:
        brief, cost = _research(task, context, subject.language)
    except (ToolboxError, ValidationError) as exc:
        raise _failure(exc) from exc
    return AugmentResult(block=render(brief), cost=cost, memory_writes=[_entry(task, subject, brief)])


def _subject(task: Any, subject_ref: str, context: dict) -> _Subject:
    module = (getattr(task, "scope_spec", None) or {}).get("module", "")
    channel = (task.params or {}).get("channel", "")
    return _Subject(channel=channel, module=module, ref=subject_ref, language=str(context.get("language") or ""))


@dataclass
class _Spend:
    """The costs of the research calls; one unknown cost makes the total unknown."""

    costs: list[Decimal | None] = field(default_factory=list)

    def take(self, costed: toolbox_calls.Costed) -> Any:
        self.costs.append(costed.cost)
        return costed.value

    def total(self) -> Decimal | None:
        return None if None in self.costs else sum(self.costs, Decimal(0))


def _research(task: Any, context: dict, language: str) -> tuple[dict, Decimal | None]:
    spend = _Spend()
    item = fenced("item_context", _dumps(context))
    seeds = _seeds(SeedsAnswer.model_validate(spend.take(_complete(task, "seeds", SEEDS_SYSTEM, item))))
    market = {"language_code": language} if language else {}
    volumes = _volumes(_items(spend.take(_seo(task, "search-volume/", {"keywords": seeds, **market}))))
    ideas = _items(spend.take(_seo(task, "keyword-ideas/", {"keywords": seeds, **market})))
    serps = {
        phrase: _serp(spend.take(_seo(task, "serp/", {"keyword": phrase, "depth": SERP_DEPTH, **market})))
        for phrase in _top(seeds, volumes)
    }
    evidence = {"candidates": [{"phrase": s, "search_volume": volumes.get(s)} for s in seeds]}
    evidence |= {"ideas": _ideas(ideas), "serp": serps}
    data = join_prompts(item, fenced("keyword_data", _dumps(evidence)))
    answer = EvaluateAnswer.model_validate(spend.take(_complete(task, "evaluate", EVALUATE_SYSTEM, data)))
    return _brief(answer, volumes | _volumes(ideas), language), spend.total()


_SCHEMAS = {"seeds": SEEDS_SCHEMA, "evaluate": EVALUATE_SCHEMA}


def _complete(task: Any, step: str, system: str, data: str) -> toolbox_calls.Costed:
    return toolbox_calls.complete(
        system=system,
        user=join_prompts(DATA_NOTE, data),
        json_schema=_SCHEMAS[step],
        model="",
        kind=f"seo_brief.{step}",
        task=task,
    )


def _seo(task: Any, endpoint: str, payload: dict) -> toolbox_calls.Costed:
    return toolbox_calls.seo(endpoint, payload, task=task)


def _items(data: Any) -> list[KeywordItem]:
    return SeoItems.model_validate(data).items


def _seeds(answer: SeedsAnswer) -> list[str]:
    seeds = list(dict.fromkeys(_phrase(seed) for seed in answer.seeds))
    if not (seeds := [seed for seed in seeds if seed]):
        raise SubjectError("SEO_BRIEF_NO_SEEDS", "no seed phrases proposed")
    return seeds[:SEEDS_MAX]


def _phrase(text: str) -> str:
    return " ".join(text.split()).lower()


def _volumes(items: list[KeywordItem]) -> dict[str, int | None]:
    return {_phrase(item.keyword): item.search_volume for item in items}


def _top(seeds: list[str], volumes: dict[str, int | None]) -> list[str]:
    """The `SERP_TOP` seeds with the highest volume (unknown counts as 0, ties keep the seed order)."""
    return sorted(seeds, key=lambda seed: -(volumes.get(seed) or 0))[:SERP_TOP]


def _serp(data: Any) -> dict:
    serp = SerpData.model_validate(data)
    return {
        "organic": [result.model_dump() for result in serp.organic[:ORGANIC_KEPT]],
        "people_also_ask": serp.people_also_ask,
        "ai_overview": serp.ai_overview,
    }


def _ideas(items: list[KeywordItem]) -> list[dict]:
    ranked = sorted(items, key=lambda item: -(item.search_volume or 0))[:IDEAS_KEPT]
    return [item.model_dump() for item in ranked]


def _brief(answer: EvaluateAnswer, volumes: dict[str, int | None], language: str) -> dict:
    chosen = [answer.primary, *answer.secondary]
    search_volume = {phrase: volumes.get(_phrase(phrase)) for phrase in chosen}
    return answer.model_dump() | {"search_volume": search_volume, "language": language}


def render(brief: dict) -> str:
    """The brief as short lines for a prompt and the memory text."""
    lines = [
        f"Primary phrase: {brief.get('primary', '')}",
        f"Secondary phrases: {', '.join(brief.get('secondary') or [])}",
        f"Search intent: {brief.get('intent', '')}",
        f"Search volume: {_dumps(brief.get('search_volume') or {})}",
        f"SERP notes: {brief.get('serp_notes', '')}",
        f"Content gaps: {'; '.join(brief.get('gaps') or [])}",
    ]
    if language := brief.get("language"):
        lines.append(f"Researched for language: {language}")
    return "\n".join(lines)


def _entry(task: Any, subject: _Subject, brief: dict) -> MemoryEntry:
    task_id = getattr(task, "pk", None)
    return MemoryEntry(
        channel=subject.channel,
        subject_module=subject.module,
        subject_ref=_memory_ref(task, subject),
        kind=MEMORY_KIND,
        title=f"SEO brief: {brief['primary']}",
        text=render(brief),
        data=brief,
        source=f"task:{task_id}" if task_id else "",
        expires_at=timezone.now() + TTL,
        created_by=getattr(task, "requested_by", None),
    )


def _memory_ref(task: Any, subject: _Subject) -> str:
    if _scope(task) != "category":
        return subject.ref
    return memory_service.category_of(subject.module, subject.ref) or subject.ref


def _scope(task: Any) -> str:
    if scope := (task.params or {}).get("seo_scope"):
        return scope
    worker = worker_service.find_active(getattr(task, "type", ""))
    return "category" if worker and worker.kind in CATEGORY_SCOPE_KINDS else "subject"


def _failure(exc: Exception) -> SubjectError:
    code = toolbox_calls.failure_code(exc) if isinstance(exc, ToolboxError) else "BAD_RESPONSE"
    return SubjectError(f"SEO_BRIEF_{code}", "seo_brief research failed")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
