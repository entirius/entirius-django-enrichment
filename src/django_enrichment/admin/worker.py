# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.contrib import admin

from django_enrichment.models import Worker
from django_enrichment.services import worker_service


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "kind", "mode", "target_module", "model", "version", "active")
    list_filter = ("kind", "mode", "target_module", "active")
    search_fields = ("key", "label")
    readonly_fields = ("version", "created_at", "modified_at")

    def save_model(self, request, obj, form, change) -> None:
        # ModelAdmin never calls Model.clean() on its own; the service runs full_clean (kind Options)
        # and bumps `version` when the calibration (system_prompt / model / options) changed.
        worker_service.save_worker(obj)
