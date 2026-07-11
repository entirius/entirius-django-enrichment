# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.contrib import admin

from django_enrichment.models import ContentProposal


@admin.register(ContentProposal)
class ContentProposalAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "subject_label",
        "target_module",
        "target_kind",
        "subject_ref",
        "status",
        "confidence",
        "batch_id",
        "created_at",
    )
    list_filter = ("status", "target_module", "target_kind")
    search_fields = ("subject_ref", "subject_label", "batch_id", "external_ref")
    readonly_fields = ("locator_hash", "created_at", "modified_at")
    raw_id_fields = ("task", "reviewed_by")
