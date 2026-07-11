# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel

from django_enrichment.enums import TaskStatus


class EnrichmentTask(BaseModel):
    """Thin work order for the enrichment bus (analysis doc, Encja 1).

    Self-describing via `params` (visible in CMS) but opaque to this module — only the
    external worker (n8n) reads the instruction. The table grows with the number of
    commands, not the catalogue: "regenerate 50k descriptions" is a single row whose
    targets are resolved lazily by the adapter (etap-03/05).
    """

    type = models.CharField(max_length=64, db_index=True)
    params = models.JSONField(default=dict, blank=True)
    scope_spec = models.JSONField(default=dict, blank=True)
    batch_key = models.CharField(max_length=128, blank=True, default="", db_index=True)
    status = models.CharField(max_length=20, choices=TaskStatus.choices, default=TaskStatus.OPEN, db_index=True)
    requested_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    counts = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "type"], name="enr_task_status_type_idx")]

    def __str__(self) -> str:
        return f"EnrichmentTask(type={self.type}, status={self.status})"
