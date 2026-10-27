# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid

from django.db import models
from django_utils.models.base_model import BaseModel

from django_enrichment.enums import RunStatus


def new_uid() -> str:
    return uuid.uuid4().hex


class WorkflowRun(BaseModel):
    """One execution of a `WorkflowDefinition` for a brief (`inputs`).

    `steps_state` is an ordered list — the definition's steps frozen at start, each with its state
    `{status, task_id, proposal_id}`; `workflow_service` is its only writer. Step outputs are proposals on
    the subject `run:<uid>`; `subject_ref` is what the final step created (the document's ref). `cost_usd`
    sums the run's tasks (null = nothing known), `cost_complete` turns false once one of them has an
    unknown cost.
    """

    uid = models.CharField(max_length=32, unique=True, default=new_uid, editable=False)
    definition = models.ForeignKey(
        "django_enrichment.WorkflowDefinition", on_delete=models.PROTECT, related_name="runs"
    )
    channel = models.CharField(max_length=64, blank=True, default="")
    inputs = models.JSONField(default=dict, blank=True)
    subject_ref = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.RUNNING, db_index=True)
    steps_state = models.JSONField(default=list, blank=True)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=8, null=True, blank=True)
    cost_complete = models.BooleanField(default=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"WorkflowRun(uid={self.uid}, status={self.status})"
