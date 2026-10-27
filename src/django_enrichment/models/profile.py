# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel


class Profile(BaseModel):
    """The company profile (contract A4 § 2) — one row per scope.

    `channel` blank = the whole instance; otherwise a channel overlay that holds only the sections that differ.
    `data` follows `schemas/profile_schema.json` (`schema_version`); `profile_service` is its only writer and
    snapshots every save as a `ProfileVersion` (`version` = the latest snapshot's number).
    """

    channel = models.CharField(max_length=64, unique=True, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    schema_version = models.PositiveIntegerField(default=1)
    version = models.PositiveIntegerField(default=0)
    updated_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Instance row only: check every new AI text proposal against the checklists (a run may override it,
    # `params.qa_check`). A setting, not profile content — not versioned, never in a prompt.
    qa_check = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"Profile(channel={self.channel or '*'}, v{self.version})"


class ProfileVersion(BaseModel):
    """A snapshot of a profile's `data` after a save; restoring one writes a new version."""

    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    data = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "version"], name="enr_profile_version_uniq"),
        ]

    def __str__(self) -> str:
        return f"ProfileVersion(profile={self.profile_id}, v{self.version})"
