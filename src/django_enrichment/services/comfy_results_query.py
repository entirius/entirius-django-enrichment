# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""What the `comfy` workers found, for the CMS: a filtered list of `ComfyResult`, the verdict totals, and per row the
score the worker's thresholds compare and a thumbnail of the very file that was checked.

The score is `metrics[<the worker's options.score>]`; sorting by it needs one worker (each worker names its own
score). The thumbnail comes from the optional adapter extra `pictures_by_sha1(*, sha1s) -> {sha1: url}` of the
worker's target module, one call per page and module; without the extra (or once the file left the module) it is
blank.
"""

from dataclasses import dataclass, replace

from django.db.models import Count, FloatField, QuerySet
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from django_enrichment.adapters import get_adapter
from django_enrichment.models import ComfyResult, Worker

ORDERINGS = frozenset({"-checked_at", "checked_at", "score", "-score"})
_ORDER_FIELDS = {"-checked_at": "-modified_at", "checked_at": "modified_at", "score": "score", "-score": "-score"}


@dataclass(frozen=True)
class ResultFilter:
    worker: str = ""
    verdict: str = ""
    source: str = ""
    reason: str = ""
    subject_ref: str = ""
    task: int | None = None


def list_results(where: ResultFilter, ordering: str = "-checked_at") -> QuerySet[ComfyResult]:
    """The matching results, newest first by default. Raises `ValueError` for an unknown ordering and for a score
    ordering without one worker."""
    if ordering not in ORDERINGS:
        raise ValueError(f"ordering must be one of {sorted(ORDERINGS)}")
    rows = _filtered(where)
    if ordering.lstrip("-") == "score":
        if not where.worker:
            raise ValueError("sorting by score needs a worker")
        rows = rows.annotate(score=_score_expression(where.worker))
    return rows.order_by(_ORDER_FIELDS[ordering], "-pk")


def verdict_totals(where: ResultFilter) -> dict[str, int]:
    """{verdict: n} of the matching results (the verdict filter itself is ignored: the totals are the chips)."""
    rows = _filtered(replace(where, verdict=""))
    return dict(rows.values_list("verdict").annotate(n=Count("pk")).order_by())


def describe(rows: list[ComfyResult]) -> list[dict]:
    """The API view of a page: each row with its score and thumbnail."""
    workers = {w.key: w for w in Worker.objects.filter(key__in={row.worker_key for row in rows})}
    thumbnails = _thumbnails(rows, workers)
    return [_row(row, workers.get(row.worker_key), thumbnails) for row in rows]


def _filtered(where: ResultFilter) -> QuerySet[ComfyResult]:
    exact = {
        "worker_key": where.worker,
        "verdict": where.verdict,
        "source": where.source,
        "reason": where.reason,
        "subject_ref": where.subject_ref,
    }
    rows = ComfyResult.objects.filter(**{field: value for field, value in exact.items() if value})
    return rows if where.task is None else rows.filter(task_id=where.task)


def _score_key(worker: Worker | None) -> str:
    return (worker.options or {}).get("score", "score") if worker is not None else "score"


def _score_expression(worker_key: str) -> Cast:
    key = _score_key(Worker.objects.filter(key=worker_key).first())
    return Cast(KeyTextTransform(key, "metrics"), FloatField())


def _thumbnails(rows: list[ComfyResult], workers: dict[str, Worker]) -> dict[tuple[str, str], str]:
    """{(module, sha1): url}, one adapter call per module on the page."""
    by_module: dict[str, set[str]] = {}
    for row in rows:
        if (worker := workers.get(row.worker_key)) is not None:
            by_module.setdefault(worker.target_module, set()).add(row.input_sha1)
    urls: dict[tuple[str, str], str] = {}
    for module, sha1s in by_module.items():
        lookup = getattr(get_adapter(module), "pictures_by_sha1", None)
        found = lookup(sha1s=sorted(sha1s)) if lookup is not None else {}
        urls.update({(module, sha1): url for sha1, url in found.items()})
    return urls


def _row(row: ComfyResult, worker: Worker | None, thumbnails: dict[tuple[str, str], str]) -> dict:
    score = row.metrics.get(_score_key(worker))
    module = worker.target_module if worker is not None else ""
    return {
        "id": row.pk,
        "worker_key": row.worker_key,
        "target_module": module,
        "subject_ref": row.subject_ref,
        "input_sha1": row.input_sha1,
        "source": row.source,
        "verdict": row.verdict,
        "reason": row.reason,
        "score": score if isinstance(score, int | float) else None,
        "metrics": row.metrics,
        "worker_version": row.worker_version,
        "task_id": row.task_id,
        "checked_at": row.modified_at,
        "thumbnail_url": thumbnails.get((module, row.input_sha1), ""),
    }
