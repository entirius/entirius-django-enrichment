# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.core.exceptions import ValidationError
from django.db import models
from django_utils.models.base_model import BaseModel
from pydantic import ValidationError as PydanticValidationError
from pydantic import create_model

from django_enrichment.models.worker import KEY_VALIDATOR, Worker, _messages


class TaskTemplate(BaseModel):
    """A saved run of a worker ("Meta descriptions"): preset `params` and `rules` for `POST tasks/`.

    `params` is validated in `clean()` against the worker kind's `Params` with `channel` optional (the
    run supplies it). Rule limits and the merge into a task live in `template_service`.
    """

    key = models.CharField(max_length=64, unique=True, validators=[KEY_VALIDATOR])
    label = models.CharField(max_length=128)
    worker = models.ForeignKey(Worker, on_delete=models.PROTECT, related_name="templates")
    params = models.JSONField(default=dict, blank=True)
    rules = models.JSONField(default=list, blank=True)
    position = models.IntegerField(default=0)
    active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["position", "key"]

    def clean(self) -> None:
        from django_enrichment.kinds import get_kind

        if self.worker_id is None:
            return
        params_model = get_kind(self.worker.kind).Params
        optional_channel = create_model("TemplateParams", __base__=params_model, channel=(str | None, None))
        try:
            optional_channel.model_validate(self.params)
        except PydanticValidationError as exc:
            raise ValidationError({"params": _messages(exc)}) from exc

    def __str__(self) -> str:
        return f"TaskTemplate(key={self.key}, worker={self.worker_id})"
