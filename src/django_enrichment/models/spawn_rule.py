# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel


class SpawnRule(BaseModel):
    """Operator config mapping one module-side quality check to a gap-mode task (etap-13).

    Used by both spawn surfaces: the CMS "Run now" button and the `enrichment.gap_autospawn`
    beat (`auto=True`). ``check_key`` is a plain string keying the module's own rule catalogue
    (PIM: ``GapDefinition.key``) — deliberately no FK and no local validation; an unknown key
    fails loud (`ValueError`) at resolve time, inside the adapter. That keeps the bus and the
    source module decoupled.

    ``limit`` / ``cooldown_days`` are nullable: NULL = the module setting
    (`ENRICHMENT_SPAWN_DEFAULT_LIMIT` / `ENRICHMENT_SPAWN_COOLDOWN_DAYS`), resolved at run time.
    """

    key = models.SlugField(max_length=64, unique=True)
    module = models.CharField(max_length=32)
    check_key = models.CharField(max_length=64)
    params = models.JSONField(default=dict, blank=True)
    scope = models.JSONField(default=dict, blank=True)
    task_type = models.CharField(max_length=64)
    task_params = models.JSONField(default=dict, blank=True)
    limit = models.PositiveIntegerField(null=True, blank=True)
    cooldown_days = models.PositiveIntegerField(null=True, blank=True)
    auto = models.BooleanField(default=False, db_index=True)
    active = models.BooleanField(default=True, db_index=True)

    def __str__(self) -> str:
        return f"SpawnRule(key={self.key}, module={self.module}, check={self.check_key})"
