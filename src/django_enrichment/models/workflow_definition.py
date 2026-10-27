# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.core.exceptions import ValidationError
from django.db import models
from django_utils.models.base_model import BaseModel

from django_enrichment.models.worker import KEY_VALIDATOR


class WorkflowDefinition(BaseModel):
    """Ordered steps (templates) with gates that turn a brief into artifacts and, at the end, a document.

    `steps`: `[{key, label, template, needs, gate: review|auto, output: artifact|questions|document}]`;
    `inputs_schema`: JSON Schema of a run's brief. Validated in `clean()` (`workflow_spec`). Seeded or edited
    in Django admin; a run copies the steps at start, so editing a definition never changes a running run.
    """

    key = models.CharField(max_length=64, unique=True, validators=[KEY_VALIDATOR])
    label = models.CharField(max_length=128)
    target_module = models.CharField(max_length=32, help_text="Adapter key the final document goes to")
    inputs_schema = models.JSONField(default=dict, blank=True)
    steps = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["key"]

    def clean(self) -> None:
        from django_enrichment.workflow_spec import definition_errors

        errors = definition_errors(steps=self.steps, inputs_schema=self.inputs_schema, target_module=self.target_module)
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"WorkflowDefinition(key={self.key})"
