# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django import forms
from django.contrib import admin

from django_enrichment.models import TaskTemplate
from django_enrichment.services import template_service
from django_enrichment.services.rule_limits import validate_rules


class TaskTemplateForm(forms.ModelForm):
    """The ModelForm runs `TaskTemplate.clean()` (params vs the worker kind); rule limits are checked here."""

    class Meta:
        model = TaskTemplate
        fields = ("key", "label", "worker", "params", "rules", "position", "active")

    def clean_rules(self) -> list[str]:
        rules = self.cleaned_data.get("rules") or []
        if not isinstance(rules, list):
            raise forms.ValidationError("rules must be a list of strings")
        try:
            return validate_rules([str(rule) for rule in rules])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc


@admin.register(TaskTemplate)
class TaskTemplateAdmin(admin.ModelAdmin):
    form = TaskTemplateForm
    list_display = ("key", "label", "worker", "position", "active")
    list_filter = ("active", "worker__target_module")
    search_fields = ("key", "label")
    list_select_related = ("worker",)
    readonly_fields = ("created_at", "modified_at")

    def save_model(self, request, obj, form, change) -> None:
        template_service.save_template(obj)
