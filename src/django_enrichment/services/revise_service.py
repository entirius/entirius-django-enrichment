# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Correction of a proposal — one stateless toolbox completion per correction (contract A3 § 1).

`revise` validates (revisable status, scope, instruction), builds the prompt (`revise_prompt`),
picks the answer schema by value shape (`revise_values`), calls the toolbox and saves the result
as an `ai` revision through `revision_service`. The `variants` quick action returns candidate
values and saves nothing. Toolbox errors propagate (the API maps them).
"""

from django.contrib.auth.models import AbstractBaseUser

from django_enrichment.enums import RevisionAuthor
from django_enrichment.kinds.base import join_prompts
from django_enrichment.models import ContentProposal, ProposalRevision
from django_enrichment.services import (
    revise_prompt,
    revise_values,
    revision_service,
    rules_service,
    toolbox_calls,
    worker_service,
)
from django_enrichment.services.revise_prompt import VARIANTS, Correction
from django_enrichment.services.revise_values import RevisionNotApplicableError

__all__ = ["RevisionNotApplicableError", "revise"]


def revise(
    proposal: ContentProposal,
    *,
    instruction: str = "",
    quick_action: str | None = None,
    scope: dict | None = None,
    user: AbstractBaseUser | None = None,
    describe_fields: revise_prompt.DescribeFields | None = None,
) -> ProposalRevision | list[dict]:
    """A new `ai` revision, or (quick action `variants`) a list of candidate values. Raises `ValueError`
    (not revisable / invalid input) or `RevisionNotApplicableError` (answer does not fit the value).
    `describe_fields` overrides the field-catalog lookup (a batch passes a cached one)."""
    revision_service.ensure_text_revisable(proposal)
    correction = Correction(revise_prompt.expand_instruction(instruction, quick_action), _scope(scope, proposal))
    meta = revise_prompt.field_meta(proposal, describe_fields)
    shape = revise_values.value_shape(proposal.proposed_value, meta)
    variants = quick_action == VARIANTS
    answer, versions = _complete(proposal, correction, meta, shape, variants=variants)
    if variants:
        return revise_values.candidates(shape, answer.value, meta, original=proposal.proposed_value)
    selection = correction.scope.get("text")
    value = revise_values.apply_answer(shape, proposal.proposed_value, answer.value, meta, selection=selection)
    return revision_service.add_revision(
        proposal,
        value,
        author_kind=RevisionAuthor.AI.value,
        author=user,
        instruction=correction.instruction,
        scope=correction.scope,
        base_value=proposal.proposed_value,
        cost_usd=answer.cost,
        profile_versions=versions,
    )


def _scope(scope: dict | None, proposal: ContentProposal) -> dict:
    """`{"type": "all"}` (default) or a selection that occurs exactly once in the current text."""
    scope = scope or {"type": "all"}
    if scope.get("type", "all") == "all":
        return {"type": "all"}
    if scope.get("type") != "selection":
        raise ValueError("scope type must be 'all' or 'selection'")
    text, current = scope.get("text") or "", proposal.proposed_value.get("text")
    if not text or not isinstance(current, str) or current.count(text) != 1:
        raise ValueError("the selection must occur exactly once in the current text")
    return {"type": "selection", "text": text}


def _complete(
    proposal: ContentProposal, correction: Correction, meta: dict | None, shape: str, *, variants: bool
) -> tuple[toolbox_calls.Costed, list[int]]:
    """The completion and the profile versions its prompt used."""
    worker = worker_service.from_source(proposal.source)
    rules = rules_service.rules_for(proposal, worker)
    company = revise_prompt.company_context(proposal, worker.kind if worker else "")
    prompt = revise_prompt.user_message(proposal, rules=rules, meta=meta, correction=correction)
    schema = revise_values.variants_schema(shape, meta) if variants else revise_values.answer_schema(shape, meta)
    answer = toolbox_calls.complete(
        system=join_prompts(worker.system_prompt if worker else "", company.instructions),
        user=join_prompts(prompt, company.data, revise_values.answer_hint(shape, variants=variants)),
        json_schema=schema,
        model=worker.model if worker else "",
        kind="revise",
        task=proposal.task,
    )
    return answer, company.versions
