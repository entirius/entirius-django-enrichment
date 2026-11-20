# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel


class ComfyResult(BaseModel):
    """What a `comfy` worker found for one product's main picture file under one workflow file — written only when it
    got an answer.

    A new main picture is a new `input_sha1` and is checked again. The same file on two products gets a row each (each
    product needs its own proposals), but the workflow runs once: the second row copies the first one's metrics.
    `workflow_hash` (sha256 of `workflow.json`) is the workflow's version:
    an edited workflow checks every file again. `metrics` come from the host's metric (or its prefilter);
    `verdict` / `reason` are the last verdict reached, recomputed from `metrics` when the worker's thresholds change
    (`worker_version`), without running the workflow again. ComfyUI down or failing leaves no row.
    """

    input_sha1 = models.CharField(max_length=40)
    worker_key = models.CharField(max_length=64)
    workflow_hash = models.CharField(max_length=64)
    subject_ref = models.CharField(max_length=255)
    source = models.CharField(max_length=16)  # `prefilter` | `workflow`
    metrics = models.JSONField(default=dict, blank=True)
    verdict = models.CharField(max_length=32)
    reason = models.CharField(max_length=64, blank=True, default="")
    worker_version = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject_ref", "input_sha1", "worker_key", "workflow_hash"], name="enr_comfy_result_key"
            )
        ]
        indexes = [
            models.Index(fields=["input_sha1", "worker_key", "workflow_hash"], name="enr_comfy_file_idx"),
            models.Index(fields=["worker_key", "verdict"], name="enr_comfy_worker_verdict_idx"),
        ]

    def __str__(self) -> str:
        return f"ComfyResult({self.worker_key}, {self.subject_ref}, {self.input_sha1[:8]}, {self.verdict})"
