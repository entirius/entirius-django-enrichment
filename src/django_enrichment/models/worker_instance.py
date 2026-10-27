# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel


class WorkerInstance(BaseModel):
    """One process that pulled work for a `Worker` (`instance` = what the consumer sends: host + pid, an n8n id)."""

    worker = models.ForeignKey("django_enrichment.Worker", on_delete=models.CASCADE, related_name="instances")
    instance = models.CharField(max_length=128)
    version = models.CharField(max_length=32, blank=True, default="")
    last_seen = models.DateTimeField()
    units_done = models.PositiveIntegerField(default=0)
    units_failed = models.PositiveIntegerField(default=0)
    consecutive_failures = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["worker", "instance"], name="enr_instance_unique")]

    def __str__(self) -> str:
        return f"WorkerInstance({self.instance})"
