# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django_utils.models.base_model import BaseModel
from pydantic import ValidationError as PydanticValidationError

from django_enrichment.enums import WorkerMode

# Fields whose change makes proposals of the old calibration incomparable — bumps `version`.
VERSIONED_FIELDS = ("system_prompt", "model", "options")

# A slug that may carry dots — keys are namespaced by module (`pim.translate`).
KEY_VALIDATOR = RegexValidator(r"^[a-z0-9][a-z0-9_.-]*$", "Lowercase letters, digits, '.', '_' and '-' only.")


class Worker(BaseModel):
    """Configured executor of one task type (`key` == `EnrichmentTask.type`).

    `kind` names the code that performs the work (`kinds.registry`); `options` is validated by the
    kind's `Options` schema in `clean()`. Edited in Django admin only. `version` is bumped by
    `worker_service.save_worker` and stamped into every proposal's `source` (`worker:<key>@v<version>`).
    """

    key = models.CharField(max_length=64, unique=True, validators=[KEY_VALIDATOR])
    label = models.CharField(max_length=128)
    kind = models.CharField(max_length=32)
    mode = models.CharField(max_length=16, choices=WorkerMode.choices, default=WorkerMode.INTERNAL)
    target_module = models.CharField(max_length=32, db_index=True)
    system_prompt = models.TextField(blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="", help_text="Blank = toolbox default")
    options = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    version = models.PositiveIntegerField(default=1, editable=False)

    def clean(self) -> None:
        from django_enrichment.kinds import get_kind

        try:
            get_kind(self.kind).Options.model_validate(self.options)
        except PydanticValidationError as exc:  # before ValueError — it is a subclass
            raise ValidationError({"options": _messages(exc)}) from exc
        except ValueError as exc:
            raise ValidationError({"kind": str(exc)}) from exc

    def __str__(self) -> str:
        return f"Worker(key={self.key}, kind={self.kind}, v{self.version})"


def _messages(exc: PydanticValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in err['loc']) or 'options'}: {err['msg']}" for err in exc.errors()]
