# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.contrib import admin

from django_enrichment.models import ComfyResult


@admin.register(ComfyResult)
class ComfyResultAdmin(admin.ModelAdmin):
    """Read-only: rows are written by the `comfy` kind only. Deleting one makes the next spawn check that file
    again (the way to re-run a single picture)."""

    list_display = ("worker_key", "subject_ref", "verdict", "reason", "source", "worker_version", "modified_at")
    list_filter = ("worker_key", "verdict", "source")
    search_fields = ("subject_ref", "input_sha1")
    readonly_fields = [field.name for field in ComfyResult._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
