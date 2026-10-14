# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Correction rules — per-proposal rules, the rule layers a correction sees, and rule promotion.

A rule is a short operator sentence ("Never mention the price"). Proposal rules live on
`ContentProposal.rules`; promotion copies one rule up to the worker (`options.rules`, version bump)
or to the task's template (`params.template`, contract A3 § 4). Target `profile` does not edit the company
profile: it leaves a `rule_suggestion` in memory for the operator to promote (contract A4 § 3).
"""

from django_enrichment.models import ContentProposal, Worker
from django_enrichment.services import memory_service, template_service, worker_service
from django_enrichment.services.rule_limits import validate_rules


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
    """Append `rule` to the template or the worker, or suggest it for the profile; returns that target's
    rules (for `profile`: the channel's active rule suggestions). Raises `ValueError`."""
    if not (cleaned := validate_rules([rule])):
        raise ValueError("rule must not be blank")
    rule = cleaned[0]
    if target == "worker":
        return _promote_to_worker(proposal, rule)
    if target == "template":
        return _promote_to_template(proposal, rule)
    if target == "profile":
        entry = memory_service.suggest_rule(proposal, rule)
        return memory_service.rule_suggestions(entry.channel)
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


def _template_rules(proposal: ContentProposal) -> list[str]:
    template = template_service.find(_template_key(proposal))
    return list(template.rules) if template else []


def _promote_to_template(proposal: ContentProposal, rule: str) -> list[str]:
    if not (key := _template_key(proposal)):
        raise ValueError("the proposal's task has no template")
    if (template := template_service.find(key)) is None:
        raise ValueError(f"template {key!r} no longer exists")
    return template_service.add_rule(template, rule)
