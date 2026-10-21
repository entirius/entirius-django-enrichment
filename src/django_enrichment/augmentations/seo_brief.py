# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Augmentation `seo_brief` (contract A4 § 5) — which phrases a text should target, from search data.

The brief is per language: `context["language"]` (the kinds set it) goes to every SEO call as `language_code`
(blank → the toolbox market defaults), is stored in the brief and must match for reuse.

1. Reuse: a fresh memory `seo_brief` in the language about the subject, then its category → that brief, cost 0.
2. Research: a completion proposes 3-5 seed phrases from the context (a workflow step's context — it has a
   `brief` — gets the topic prompt: 2-4 word head terms, not questions, for the keyword tool to expand; an item
   keeps the item prompt) → toolbox SEO `search-volume/` and
   `keyword-ideas/` (one call each) → `serp/` for the top `SERP_TOP` seeds by volume.
3. A completion evaluates intent first (SERP intent, winnability, gaps; volume breaks ties) →
   `{primary, secondary, intent, serp_notes, gaps, search_volume}`, remembered for `TTL` under the category when
   the scope is `category` (`params.seo_scope`; default for kind `write`, falls back to the subject without a
   category), else under the subject.

Every call charges the task (`toolbox_calls`); a toolbox failure or an answer that does not fit the boundary
models (`seo_answers`) fails the subject with `SEO_BRIEF_<code>`; other errors propagate to the runner.
"""

import json
import logging
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
    SEED_MAX_CHARS,
    SEEDS_SCHEMA,
    SEEDS_SYSTEM,
    TOPIC_SEEDS_SYSTEM,
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


_logger = logging.getLogger("process")


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
        return AugmentResult(block=_block(task, found.data), cost=Decimal(0))
    try:
        brief, cost = _research(task, context, subject.language)
    except (ToolboxError, ValidationError) as exc:
        raise _failure(exc) from exc
    return AugmentResult(block=_block(task, brief), cost=cost, memory_writes=[_entry(task, subject, brief)])


def _block(task: Any, brief: dict) -> str:
    """The short brief; with `params.seo_detail == "full"` also the search evidence it rests on."""
    evidence = brief.get("evidence") if (task.params or {}).get("seo_detail") == "full" else None
    return join_prompts(render(brief), render_evidence(evidence)) if evidence else render(brief)


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
    system = TOPIC_SEEDS_SYSTEM if "brief" in context else SEEDS_SYSTEM
    seeds = _seeds(SeedsAnswer.model_validate(spend.take(_complete(task, "seeds", system, item))))
    market = {"language_code": language} if language else {}
    volumes = _volumes(_items(spend.take(_seo(task, "search-volume/", {"keywords": seeds, **market}))))
    ideas = _items(spend.take(_seo(task, "keyword-ideas/", {"keywords": seeds, **market})))
    serps = _serps(task, _top(seeds, volumes), market, spend)
    evidence = {"candidates": [{"phrase": s, "search_volume": volumes.get(s)} for s in seeds]}
    evidence |= {"ideas": _ideas(ideas), "serp": serps}
    data = join_prompts(item, fenced("keyword_data", _dumps(evidence)))
    answer = EvaluateAnswer.model_validate(spend.take(_complete(task, "evaluate", EVALUATE_SYSTEM, data)))
    return _brief(answer, volumes | _volumes(ideas), language) | {"evidence": evidence}, spend.total()


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


def _serps(task: Any, phrases: list[str], market: dict, spend: _Spend) -> dict:
    """SERP notes per phrase; a phrase whose SERP call fails is left out (its cost unknown) — the brief still
    rests on volumes and ideas."""
    serps = {}
    for phrase in phrases:
        try:
            costed = _seo(task, "serp/", {"keyword": phrase, "depth": SERP_DEPTH, **market})
        except ToolboxError as exc:
            _logger.warning("seo_brief: SERP of task %s failed (%s)", task.pk, toolbox_calls.failure_code(exc))
            spend.costs.append(None)  # a failed paid call may still be billed
            continue
        serps[phrase] = _serp(spend.take(costed))
    return serps


def _items(data: Any) -> list[KeywordItem]:
    return SeoItems.model_validate(data).items


def _seeds(answer: SeedsAnswer) -> list[str]:
    """Phrases within the toolbox keyword limit; only when none fits, the overlong ones cut to whole words."""
    phrases = list(dict.fromkeys(_phrase(seed) for seed in answer.seeds))
    seeds = [seed for seed in phrases if 0 < len(seed) <= SEED_MAX_CHARS]
    if not (seeds := seeds or list(dict.fromkeys(filter(None, map(_cut, phrases))))):
        raise SubjectError("SEO_BRIEF_NO_SEEDS", "no seed phrases proposed")
    return seeds[:SEEDS_MAX]


def _cut(phrase: str) -> str:
    """`phrase` cut at the last word break within the limit; blank when it has none."""
    head = phrase[: SEED_MAX_CHARS + 1]
    return head.rsplit(" ", 1)[0] if " " in head else ""


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


def render_evidence(evidence: dict) -> str:
    """The search data a brief rests on: monthly volumes (seeds and ideas), then each SERP with people also ask."""
    volumes = {item["phrase"]: item.get("search_volume") for item in evidence.get("candidates") or []}
    for idea in evidence.get("ideas") or []:
        if volumes.get(phrase := _phrase(idea["keyword"])) is None:
            volumes[phrase] = idea.get("search_volume")
    ranked = sorted(volumes.items(), key=lambda pair: -(pair[1] or 0))
    lines = ["Monthly search volume (? = unknown):", *(f"- {p}: {v if v is not None else '?'}" for p, v in ranked)]
    serps = [_render_serp(phrase, serp) for phrase, serp in (evidence.get("serp") or {}).items()]
    return join_prompts("\n".join(lines), *serps)


def _render_serp(phrase: str, serp: dict) -> str:
    overview = {True: "yes", False: "no"}.get(serp.get("ai_overview"), "unknown")
    results = [_organic_line(index, result) for index, result in enumerate(serp.get("organic") or [], start=1)]
    lines = [f'SERP "{phrase}" (AI overview: {overview}):', *results]
    if questions := serp.get("people_also_ask"):
        lines.append(f"People also ask: {'; '.join(questions)}")
    return "\n".join(lines)


def _organic_line(index: int, result: dict) -> str:
    head = f"{result.get('rank') or index}. {result.get('domain') or '?'} - {result.get('title') or ''}"
    return f"{head}: {result['snippet']}" if result.get("snippet") else head


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
    return memory_service.category_of(subject.module, subject.ref, channel=subject.channel) or subject.ref


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
