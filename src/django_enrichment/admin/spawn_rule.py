# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.contrib import admin

from django_enrichment.models import SpawnRule


@admin.register(SpawnRule)
class SpawnRuleAdmin(admin.ModelAdmin):
    list_display = ("key", "module", "check_key", "task_type", "auto", "active", "created_at")
    list_filter = ("module", "auto", "active")
    search_fields = ("key", "check_key")
    readonly_fields = ("created_at", "modified_at")
