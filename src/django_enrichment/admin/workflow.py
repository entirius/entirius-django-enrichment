# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.contrib import admin

from django_enrichment.models import WorkflowDefinition, WorkflowRun


@admin.register(WorkflowDefinition)
class WorkflowDefinitionAdmin(admin.ModelAdmin):
    """The ModelForm runs `WorkflowDefinition.clean()` (steps, templates, inputs schema)."""

    list_display = ("key", "label", "target_module", "active")
    list_filter = ("active", "target_module")
    search_fields = ("key", "label")
    readonly_fields = ("created_at", "modified_at")


@admin.register(WorkflowRun)
class WorkflowRunAdmin(admin.ModelAdmin):
    """Read-only: runs move only through `workflow_service` (API / proposal approvals)."""

    list_display = ("uid", "definition", "status", "channel", "subject_ref", "cost_usd", "created_at")
    list_filter = ("status", "definition")
    search_fields = ("uid", "subject_ref")
    list_select_related = ("definition",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
