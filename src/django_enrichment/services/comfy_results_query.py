# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""What the `comfy` workers found, for the CMS: a filtered list of `ComfyResult`, the verdict totals, a CSV of the
whole list, and per row the score the worker's thresholds compare and a thumbnail of the very file that was checked.

By default only the latest check of each product per worker is listed (`latest`): an older file of the product, or
an older run of an edited workflow, stays in the table as history. The score is `metrics[<the worker's
options.score>]`; sorting or filtering by it needs one worker (each worker names its own score). The thumbnail comes from the optional adapter extra `pictures_by_sha1(*, sha1s) -> {sha1: url}` of the
worker's target module, one call per page and module; without the extra (or once the file left the module) it is
blank.
"""

from collections.abc import Iterator
from dataclasses import dataclass, replace

from django.db.models import Count, F, FloatField, OuterRef, QuerySet, Subquery
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from django_enrichment.adapters import get_adapter
from django_enrichment.models import ComfyResult, Worker

ORDERINGS = frozenset({"-checked_at", "checked_at", "score", "-score"})
# A result without a score (a prefilter verdict) goes last both ways; PostgreSQL puts NULLs first when descending.
_ORDER_FIELDS = {
    "-checked_at": F("modified_at").desc(),
    "checked_at": F("modified_at").asc(),
    "score": F("score").asc(nulls_last=True),
    "-score": F("score").desc(nulls_last=True),
}


@dataclass(frozen=True)
class ResultFilter:
    worker: str = ""
    verdict: str = ""
    source: str = ""
    reason: str = ""
    subject_ref: str = ""
    search: str = ""
    task: int | None = None
    score_min: float | None = None
    score_max: float | None = None
    latest: bool = True


def list_results(where: ResultFilter, ordering: str = "-checked_at") -> QuerySet[ComfyResult]:
    """The matching results, newest first by default. Raises `ValueError` for an unknown ordering, and for a score
    ordering or a score range without one worker."""
    if ordering not in ORDERINGS:
        raise ValueError(f"ordering must be one of {sorted(ORDERINGS)}")
    if ordering.lstrip("-") == "score" and not where.worker:
        raise ValueError("sorting by score needs a worker")
    return _filtered(where).order_by(_ORDER_FIELDS[ordering], "-pk")


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
    if where.search:
        rows = rows.filter(subject_ref__icontains=where.search)
    if where.task is not None:
        rows = rows.filter(task_id=where.task)
    if where.latest:
        rows = rows.filter(pk=Subquery(_latest_of_product()))
    return _scored(rows, where)


def _latest_of_product() -> QuerySet[ComfyResult]:
    """The pk of the newest result of the same product and worker (a check of the current file under the current run)."""
    same = ComfyResult.objects.filter(worker_key=OuterRef("worker_key"), subject_ref=OuterRef("subject_ref"))
    return same.order_by("-modified_at", "-pk").values("pk")[:1]


def _scored(rows: QuerySet[ComfyResult], where: ResultFilter) -> QuerySet[ComfyResult]:
    """With one worker, every row carries `score`; a score range without one is refused."""
    if not where.worker:
        if where.score_min is not None or where.score_max is not None:
            raise ValueError("a score range needs a worker")
        return rows
    rows = rows.annotate(score=_score_expression(where.worker))
    if where.score_min is not None:
        rows = rows.filter(score__gte=where.score_min)
    if where.score_max is not None:
        rows = rows.filter(score__lte=where.score_max)
    return rows


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
        if (lookup := _lookup(module)) is None:
            continue
        found = lookup(sha1s=sorted(sha1s))
        urls.update({(module, sha1): url for sha1, url in found.items()})
    return urls


def _lookup(module: str):
    """The module's `pictures_by_sha1`, or None: no such extra, or no adapter registered any more (a worker left
    behind by a removed module must not break the list)."""
    try:
        return getattr(get_adapter(module), "pictures_by_sha1", None)
    except ValueError:
        return None


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


EXPORT_CAP = 50_000
_EXPORT_COLUMNS = ("worker_key", "subject_ref", "verdict", "reason", "score", "source", "worker_version", "task_id")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def export_rows(where: ResultFilter, ordering: str = "-checked_at") -> Iterator[list]:
    """The matching results as CSV rows (header first, at most `EXPORT_CAP`): the fixed columns, `checked_at`,
    `input_sha1`, then one column per metric key. A text cell a spreadsheet would read as a formula is prefixed `'`."""
    rows = list_results(where, ordering)[:EXPORT_CAP]
    keys = sorted({key for metrics in rows.values_list("metrics", flat=True) for key in (metrics or {})})
    workers = {w.key: w for w in Worker.objects.all()}  # a handful of rows; each worker names its own score
    yield [*_EXPORT_COLUMNS, "checked_at", "input_sha1", *keys]
    for row in rows.iterator(chunk_size=1000):
        score = row.metrics.get(_score_key(workers.get(row.worker_key)))
        fixed = [getattr(row, name, None) for name in _EXPORT_COLUMNS]
        fixed[_EXPORT_COLUMNS.index("score")] = score if isinstance(score, int | float) else ""
        cells = [*fixed, row.modified_at.isoformat(), row.input_sha1, *(row.metrics.get(key, "") for key in keys)]
        yield [_safe_cell(cell) for cell in cells]


def _safe_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return f"'{value}"
    return value
