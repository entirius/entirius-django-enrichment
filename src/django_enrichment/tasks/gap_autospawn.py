# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Periodic gap autospawn (etap-13) — `enrichment.gap_autospawn`.

Runs every active `auto=True` SpawnRule through `run_rule`. Per-rule dedup (the deterministic
`gaprule:` batch_key + open/in_progress lookup) and the probe-before-spawn live in the service,
so this beat is a dumb loop: classify each rule's outcome, never let one broken rule (an adapter
raising on an unknown check, a module down) kill the rest.

The host schedules it in `CELERY_BEAT_SCHEDULE` (e.g. every 30 min) on the enrichment queue —
the interval doubles as the natural debounce of auto-spawning.
"""

import logging
from typing import Any

from celery import shared_task

logger = logging.getLogger("process")


@shared_task(name="enrichment.gap_autospawn")
def gap_autospawn_task() -> dict[str, Any]:
    from django_enrichment.models import SpawnRule
    from django_enrichment.services import spawn_rule_service

    summary: dict[str, Any] = {"rules": 0, "spawned": [], "already_running": [], "empty": [], "failed": []}
    for rule in SpawnRule.objects.filter(auto=True, active=True).order_by("key"):
        summary["rules"] += 1
        try:
            if spawn_rule_service.find_running_task(rule.key) is not None:
                summary["already_running"].append(rule.key)
                continue
            task = spawn_rule_service.run_rule(rule)
        except Exception:
            logger.exception("gap_autospawn: rule %s failed", rule.key)
            summary["failed"].append(rule.key)
            continue
        if task is None:
            summary["empty"].append(rule.key)
        else:
            summary["spawned"].append(rule.key)
    return summary
