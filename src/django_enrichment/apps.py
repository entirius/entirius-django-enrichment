# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.apps import AppConfig


class EnrichmentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_enrichment"
    verbose_name = "Enrichment"
    is_volkanos = True
    # The first two copied 1:1 from entirius-django-access cf538d2 catalogue defaults;
    # the access defaults stay until this module's release.
    access_areas = [
        {"key": "enrichment.rules", "label": "Enrichment spawn rules"},
        {"key": "enrichment.proposals", "label": "Enrichment tasks and proposals"},
        # The profile is injected into every system prompt: editing it is narrower than reviewing proposals.
        {"key": "enrichment.profile", "label": "Enrichment company profile and memory"},
    ]
    # Every admin view carries its access_area; no route needs a path rule.
    access_route_rules = []

    def ready(self) -> None:
        from django_enrichment.kinds import register_kind
        from django_enrichment.kinds.compose import ComposeKind
        from django_enrichment.kinds.field import FieldKind
        from django_enrichment.kinds.generate import GenerateKind
        from django_enrichment.kinds.pick import PickKind
        from django_enrichment.kinds.questions import QuestionsKind
        from django_enrichment.kinds.translate import TranslateKind
        from django_enrichment.kinds.video import VideoKind
        from django_enrichment.kinds.write import WriteKind

        register_kind("translate", TranslateKind)
        register_kind("field", FieldKind)
        register_kind("video", VideoKind)
        register_kind("write", WriteKind)
        register_kind("pick", PickKind)
        register_kind("generate", GenerateKind)
        register_kind("questions", QuestionsKind)
        register_kind("compose", ComposeKind)
