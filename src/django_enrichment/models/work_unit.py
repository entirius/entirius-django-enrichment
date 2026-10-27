# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel

from django_enrichment.enums import TaskLane, UnitStatus


class WorkUnit(BaseModel):
    """A small slice of a task (a few subjects) claimed under a lease by one worker instance.

    `events` is the capped trail of the last state changes (`queue_log`), `last_error` the plain-words reason of
    the latest failure (`{code, message}`, never exception text), `outcome` the result summary kept so a repeated
    `complete` answers the same. `lease_token` is cleared when the lease is lost and kept once the unit is done.
    """

    task = models.ForeignKey("django_enrichment.EnrichmentTask", on_delete=models.CASCADE, related_name="work_units")
    worker_key = models.CharField(max_length=64)
    subject_refs = models.JSONField(default=list)
    lane = models.CharField(max_length=8, choices=TaskLane.choices, default=TaskLane.QUEUE)
    status = models.CharField(max_length=12, choices=UnitStatus.choices, default=UnitStatus.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True, unique=True)
    leased_by = models.CharField(max_length=128, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)
    not_before = models.DateTimeField(null=True, blank=True)
    last_error = models.JSONField(default=dict, blank=True)
    outcome = models.JSONField(default=dict, blank=True)
    events = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "lane", "created_at"], name="enr_unit_status_lane_idx"),
            models.Index(fields=["worker_key", "status"], name="enr_unit_worker_status_idx"),
        ]

    def __str__(self) -> str:
        return f"WorkUnit(task={self.task_id}, status={self.status}, attempts={self.attempts})"
