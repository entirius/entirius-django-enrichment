# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.contrib import admin

from django_enrichment.models import EnrichmentTask


@admin.register(EnrichmentTask)
class EnrichmentTaskAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "status", "batch_key", "requested_by", "created_at")
    list_filter = ("status", "type")
    search_fields = ("type", "batch_key")
    readonly_fields = ("created_at", "modified_at")
    raw_id_fields = ("requested_by",)
