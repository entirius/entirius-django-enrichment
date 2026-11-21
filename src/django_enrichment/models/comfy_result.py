# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel


class ComfyResult(BaseModel):
    """What a `comfy` worker found for one product's main picture file under one workflow file — written only when it
    got an answer.

    A new main picture is a new `input_sha1` and is checked again. `run_hash` (sha256 of `workflow.json` and of the
    options that shape a result, `kinds.comfy.RUN_FIELDS`) is the run's version: an edited workflow, metric, score,
    prefilter or output checks every picture again. `metrics` come from the host's metric (or its prefilter);
    `verdict` / `reason` are the last verdict reached, recomputed from `metrics` when the worker's thresholds change
    (`worker_version`), without running the workflow again. ComfyUI down or failing leaves no row.
    """

    input_sha1 = models.CharField(max_length=40)
    worker_key = models.CharField(max_length=64)
    run_hash = models.CharField(max_length=64)
    subject_ref = models.CharField(max_length=255)
    source = models.CharField(max_length=16)  # `prefilter` | `workflow`
    metrics = models.JSONField(default=dict, blank=True)
    verdict = models.CharField(max_length=32)
    reason = models.CharField(max_length=64, blank=True, default="")
    worker_version = models.PositiveIntegerField()
    # `metrics[<the worker's options.score>]` when it is a number, else NULL (a prefilter verdict): the value the list
    # sorts and filters by. The score key is a run-shaping option, so a row's score never changes meaning.
    score = models.FloatField(null=True, blank=True)
    # The task that judged this file last (a later task re-judging a stored result takes it over); NULL for rows
    # written before 2.5.0 and once that task is pruned.
    task = models.ForeignKey(
        "django_enrichment.EnrichmentTask",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="comfy_results",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject_ref", "input_sha1", "worker_key", "run_hash"], name="enr_comfy_result_key"
            )
        ]
        indexes = [
            models.Index(fields=["worker_key", "run_hash", "input_sha1"], name="enr_comfy_run_idx"),
            models.Index(fields=["worker_key", "verdict"], name="enr_comfy_worker_verdict_idx"),
            models.Index(fields=["worker_key", "subject_ref"], name="enr_comfy_worker_subject_idx"),
            models.Index(fields=["worker_key", "score"], name="enr_comfy_worker_score_idx"),
        ]

    def __str__(self) -> str:
        return f"ComfyResult({self.worker_key}, {self.subject_ref}, {self.input_sha1[:8]}, {self.verdict})"
