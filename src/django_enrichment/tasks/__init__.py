"""Celery task package.

`autodiscover_tasks()` imports `<app>.tasks` (this package), so the task module must be imported here
for its `@shared_task`s to register with the host's Celery app — otherwise the worker rejects them with
"Received unregistered task". (Without this, `enrichment.apply_*` only ever ran eager in tests.)
"""

from django_enrichment.tasks.apply_tasks import (
    apply_chunk_task,
    apply_proposals_task,
    dispatch_canvas,
    finalize_batch_task,
)
from django_enrichment.tasks.cleanup import prune_task
from django_enrichment.tasks.gap_autospawn import gap_autospawn_task

__all__ = [
    "apply_chunk_task",
    "apply_proposals_task",
    "dispatch_canvas",
    "finalize_batch_task",
    "gap_autospawn_task",
    "prune_task",
]
