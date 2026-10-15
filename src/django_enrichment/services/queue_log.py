# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Work-queue log — one `enrichment.queue` key=value line per event, mirrored into the unit's capped trail.

Never pass subject content, proposal values or secrets here: ids, counters and stable codes only.
"""

import logging
from typing import Any

from django.utils import timezone

from django_enrichment.models import WorkUnit
from django_enrichment.services import queue_config

_logger = logging.getLogger("enrichment.queue")


def _line(event: str, fields: dict[str, Any]) -> str:
    parts = [f"event={event}"]
    parts += [f"{key}={str(value).replace(' ', '_')}" for key, value in fields.items() if value not in ("", None)]
    return " ".join(parts)


def log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """A queue event that has no unit (reap summary, escalation, pause)."""
    _logger.log(level, _line(event, fields))


def unit_event(
    unit: WorkUnit, event: str, *, instance: str = "", code: str = "", detail: str = "", duration_ms: int | None = None
) -> None:
    """Log the event of `unit` and append it to `unit.events` (the caller saves the unit)."""
    level = logging.WARNING if event in ("failed", "lease_expired", "lease_lost") else logging.INFO
    log(
        event,
        level,
        unit=unit.pk,
        task=unit.task_id,
        worker=unit.worker_key,
        instance=instance or unit.leased_by,
        attempt=unit.attempts,
        lane=unit.lane,
        duration_ms=duration_ms,
        code=code,
    )
    trail = [
        *unit.events,
        {
            "at": timezone.now().isoformat(),
            "event": event,
            "instance": instance or unit.leased_by,
            "attempt": unit.attempts,
            "detail": detail or code,
        },
    ]
    unit.events = trail[-queue_config.get("ENRICHMENT_EVENTS_CAP") :]
