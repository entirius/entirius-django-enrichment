# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.apps import AppConfig


class EnrichmentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_enrichment"
    verbose_name = "Enrichment"
    is_volkanos = True
    # Copied 1:1 from entirius-django-access cf538d2 catalogue defaults;
    # the access defaults stay until this module's release.
    access_areas = [
        {"key": "enrichment.rules", "label": "Enrichment spawn rules"},
        {"key": "enrichment.proposals", "label": "Enrichment tasks and proposals"},
    ]
    # Every admin view carries its access_area; no route needs a path rule.
    access_route_rules = []
