# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel

from django_enrichment.enums import MemoryKind, MemoryStatus


class MemoryEntry(BaseModel):
    """Something the studio remembers (contract A4 § 3): an SEO brief, an operator answer, a suggestion.

    `channel` blank = the instance; `subject_module` + `subject_ref` blank = general (a category is a subject
    too). `source` says where it came from (`task:<id>`, `run:<uid>/<step>`, `proposal:<id>`). Expired or
    non-active entries are never recalled.
    """

    channel = models.CharField(max_length=64, blank=True, default="")
    subject_module = models.CharField(max_length=64, blank=True, default="")
    subject_ref = models.CharField(max_length=255, blank=True, default="")
    kind = models.CharField(max_length=32, choices=MemoryKind.choices)
    title = models.CharField(max_length=200)
    text = models.TextField(max_length=4000, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=16, choices=MemoryStatus.choices, default=MemoryStatus.ACTIVE)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["channel", "subject_module", "subject_ref", "kind"], name="enr_memory_subject_idx"),
        ]

    def __str__(self) -> str:
        return f"MemoryEntry({self.kind}: {self.title[:40]})"
