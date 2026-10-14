# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Correction rules — per-proposal rules, the rule layers a correction sees, and rule promotion.

A rule is a short operator sentence ("Never mention the price"). Proposal rules live on
`ContentProposal.rules`; promotion copies one rule up to the worker (`options.rules`, version bump)
or to the task's template (seam — templates arrive with contract A3 § 4).
"""

from django_enrichment.models import ContentProposal, Worker
from django_enrichment.services import worker_service

MAX_RULES = 20
MAX_RULE_LENGTH = 200


def validate_rules(rules: list[str]) -> list[str]:
    """Strip, drop blanks, enforce the limits. Raises `ValueError`."""
    cleaned = [rule.strip() for rule in rules if rule.strip()]
    if len(cleaned) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} rules allowed")
    if any(len(rule) > MAX_RULE_LENGTH for rule in cleaned):
        raise ValueError(f"a rule may have at most {MAX_RULE_LENGTH} characters")
    return cleaned


def set_rules(proposal: ContentProposal, rules: list[str]) -> ContentProposal:
    proposal.rules = validate_rules(rules)
    proposal.save(update_fields=["rules", "modified_at"])
    return proposal


def rules_for(proposal: ContentProposal, worker: Worker | None) -> list[str]:
    """Rule layers of a correction: proposal rules, then template rules, then worker rules (deduplicated)."""
    worker_rules = list((worker.options or {}).get("rules", [])) if worker else []
    layers = [*proposal.rules, *_template_rules(proposal), *worker_rules]
    return list(dict.fromkeys(layers))


def promote(proposal: ContentProposal, rule: str, *, target: str) -> list[str]:
    """Append `rule` to the template or the worker; returns that target's rules. Raises `ValueError`."""
    if not (cleaned := validate_rules([rule])):
        raise ValueError("rule must not be blank")
    rule = cleaned[0]
    if target == "worker":
        return _promote_to_worker(proposal, rule)
    if target == "template":
        return _promote_to_template(proposal, rule)
    raise ValueError(f"unknown promotion target {target!r}")


def _promote_to_worker(proposal: ContentProposal, rule: str) -> list[str]:
    worker = worker_service.from_source(proposal.source)
    if worker is None:
        raise ValueError("the proposal was not produced by a worker")
    rules = validate_rules([*worker.options.get("rules", []), rule])
    worker.options = {**worker.options, "rules": list(dict.fromkeys(rules))}
    worker_service.save_worker(worker)
    return worker.options["rules"]


def _template_key(proposal: ContentProposal) -> str:
    return ((proposal.task.params if proposal.task else None) or {}).get("template", "")


def _template_rules(proposal: ContentProposal) -> list[str]:  # noqa: ARG001 — seam until templates exist
    return []


def _promote_to_template(proposal: ContentProposal, rule: str) -> list[str]:  # noqa: ARG001 — seam
    if not _template_key(proposal):
        raise ValueError("the proposal's task has no template")
    raise ValueError("templates are not available yet")
