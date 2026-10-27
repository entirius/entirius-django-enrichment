# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Promotion of a memory suggestion into the company profile (contract A4 § 3) — a new profile version.

`rule_suggestion` → `always_rules` (default) or a lint rule in `rules` (the entry's `data` carries `pattern`);
`fact_suggestion` → `facts` (verified today, the operator vouches for it by promoting). The entry's channel
picks the profile scope; the entry becomes `promoted` in the same transaction.
"""

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.utils import timezone

from django_enrichment.enums import MemoryKind, MemoryStatus
from django_enrichment.models import MemoryEntry, Profile
from django_enrichment.services import profile_service

ALWAYS_RULES = "always_rules"
RULES = "rules"
FACTS = "facts"
_TARGETS = {
    MemoryKind.RULE_SUGGESTION.value: (ALWAYS_RULES, RULES),
    MemoryKind.FACT_SUGGESTION.value: (FACTS,),
}


def promote(entry: MemoryEntry, *, target: str | None = None, user: AbstractBaseUser | None = None) -> Profile:
    """Write the suggestion into its channel's profile; returns the saved profile. Raises `ValueError`."""
    target = _target(entry, target)
    with transaction.atomic():
        locked = MemoryEntry.objects.select_for_update().get(pk=entry.pk)
        if locked.status != MemoryStatus.ACTIVE.value:
            raise ValueError(f"only active entries can be promoted (status: {locked.status})")
        profile = profile_service.update_profile(
            locked.channel, lambda data: _added(data, locked, target), note=f"promoted memory {locked.pk}", user=user
        )
        locked.status = MemoryStatus.PROMOTED.value
        locked.save(update_fields=["status", "modified_at"])
    entry.refresh_from_db()
    return profile


def _target(entry: MemoryEntry, target: str | None) -> str:
    if not (allowed := _TARGETS.get(entry.kind)):
        raise ValueError(f"a {entry.kind} entry cannot be promoted")
    target = target or allowed[0]
    if target not in allowed:
        raise ValueError(f"a {entry.kind} entry promotes to {' or '.join(allowed)}")
    if target == RULES and not entry.data.get("pattern"):
        raise ValueError("a lint rule needs a pattern in the entry's data")
    return target


def _added(data: dict, entry: MemoryEntry, target: str) -> dict:
    current = list(data.get(target, []))
    item = {ALWAYS_RULES: _always_rule, RULES: _lint_rule, FACTS: _fact}[target](entry)
    if item not in current:
        current.append(item)
    return {**data, target: current}


def _always_rule(entry: MemoryEntry) -> str:
    return entry.text or entry.title


def _lint_rule(entry: MemoryEntry) -> dict:
    rule = {"id": f"m{entry.pk}", "pattern": entry.data["pattern"], "message": entry.data.get("message") or entry.title}
    rule.update({key: entry.data[key] for key in ("flags", "do", "dont") if entry.data.get(key)})
    return {**rule, "active": True}


def _fact(entry: MemoryEntry) -> dict:
    fact = {
        "id": str(entry.data.get("id") or f"m{entry.pk}"),
        "label": str(entry.data.get("label") or entry.title),
        "value": str(entry.data.get("value") or entry.text),
        "source": str(entry.data.get("source") or entry.source),
        "verified_at": timezone.localdate().isoformat(),
    }
    return {**fact, **({"note": str(entry.data["note"])} if entry.data.get("note") else {})}
