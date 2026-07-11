# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""CSV import parsing for the enrichment bus (etap-11) — framework-agnostic, module-agnostic.

The CSV spawn surface uploads a 3-column file (`sku,field,type`); one file = one channel + one
language (chosen in the dialog). Rows are grouped by `type` (the work-type, e.g. `fix-attribute`,
`translate`) → one single-type `EnrichmentTask` each. The file is staged once in the bus's own
`staging_store` and streamed lazily at resolve time.

**Why the bus parses the CSV, not the adapter (decision D1):** the file lives in the bus's staging
store, which a source module must never import. `sku,field,type` is a generic shape — nothing PIM
here — so `task_service.resolve_targets` calls `stream_targets` directly for `mode:csv` and never
hands the staged ref to the adapter. Targets are generic (`{subject_ref, field}`); the worker maps
`field` to a module locator.

Format (locked with PO): UTF-8 (BOM-tolerant), comma-separated, exactly the three expected columns.
`channel`/`language` columns are out of scope (future — mixing in one file). SKU existence is NOT
checked here (format only) — it's resolved lazily downstream, so a 50k-row upload stays cheap.
"""

import csv
import io

from django.conf import settings

from django_enrichment import settings as enrichment_settings
from django_enrichment.services import staging_store

# The only accepted header, lower-cased + stripped. Extra/renamed columns are rejected so a
# `channel`/`language` column (future) can't silently change semantics today.
EXPECTED_HEADER = ("sku", "field", "type")

# Targets per `resolve_targets` page — a worker pulls one page at a time; we never materialise the
# whole file (a 50k-row CSV must not OOM the web process — R3).
CSV_PAGE_SIZE = 100


def _max_bytes() -> int:
    return getattr(settings, "ENRICHMENT_MAX_CSV_BYTES", enrichment_settings.ENRICHMENT_MAX_CSV_BYTES)


def _normalise_header(fieldnames: list[str] | None) -> tuple[str, ...]:
    return tuple((name or "").strip().lower() for name in (fieldnames or []))


def validate_and_stage(file) -> tuple[str, list[str]]:
    """Validate a CSV upload (header + per-row shape), stage it, return `(ref, distinct_types)`.

    Reads the whole upload once: enforces the byte cap, the exact 3-column header, and a non-empty
    `sku`/`field`/`type` in every row (format only — no source-module lookup). Collects the distinct
    `type` values (the task groups). Stages the original bytes via `staging_store` so the worker can
    stream them later. Raises `ValueError` (row-numbered) on any violation — nothing is staged then.
    """
    max_bytes = _max_bytes()
    size = getattr(file, "size", None)
    if size is not None and size > max_bytes:
        raise ValueError(f"CSV exceeds the {max_bytes}-byte cap")

    # Read with a hard ceiling rather than trusting `.size` (which a crafted upload can leave unset):
    # `read(max_bytes + 1)` bounds the in-memory copy regardless, so the cap is authoritative for both
    # the size-known and size-unknown paths — never an unbounded `file.read()`.
    file.seek(0)
    raw = file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"CSV exceeds the {max_bytes}-byte cap")
    if not raw:
        raise ValueError("CSV is empty")

    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    header = _normalise_header(reader.fieldnames)
    if header != EXPECTED_HEADER:
        raise ValueError(f"CSV header must be exactly {list(EXPECTED_HEADER)}, got {list(header)}")

    types: list[str] = []
    seen: set[str] = set()
    row_count = 0
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        sku = (row.get("sku") or "").strip()
        field = (row.get("field") or "").strip()
        work_type = (row.get("type") or "").strip()
        if not (sku and field and work_type):
            raise ValueError(f"row {i}: sku, field and type are all required")
        row_count += 1
        if work_type not in seen:
            seen.add(work_type)
            types.append(work_type)

    if row_count == 0:
        raise ValueError("CSV has a header but no data rows")

    file.seek(0)
    ref = staging_store.save(file)
    return ref, sorted(types)


def stream_targets(ref: str, *, work_type: str, page: int = 1) -> list[dict]:
    """Stream one page of `{subject_ref, field}` targets for a work-type from the staged CSV.

    Lazy: opens the staged file and reads it row by row, materialising only the requested page (the
    type filter + offset/limit are applied while iterating, never a full load). Returns `[]` past the
    end. The file is the single source for all N tasks spawned from one upload — each task filters to
    its own `work_type`.

    Trade-off: each page re-reads from the start to skip `offset` matching rows (no random seek into a
    CSV). For the worker pull-loop at the 10 MB / ~50k-row cap this is bounded CPU paid in the Celery
    worker, not the web process. If a much larger cap or a tight drain SLA ever lands, pre-split per
    work-type at stage time or index byte offsets.
    """
    offset = (max(page, 1) - 1) * CSV_PAGE_SIZE
    targets: list[dict] = []
    matched = 0
    with staging_store.open_file(ref) as fh:
        # `open_file` returns a Django `File` (binary). Decode line-by-line into `DictReader` instead
        # of buffering the whole file — a `\n` byte never appears inside a UTF-8 multibyte sequence,
        # so splitting on lines first is safe, and `utf-8-sig` strips a leading BOM on the first line.
        reader = csv.DictReader(line.decode("utf-8-sig") for line in fh)
        for row in reader:
            if (row.get("type") or "").strip() != work_type:
                continue
            if matched >= offset:
                targets.append(
                    {"subject_ref": (row.get("sku") or "").strip(), "field": (row.get("field") or "").strip()}
                )
            matched += 1
            if len(targets) >= CSV_PAGE_SIZE:
                break
    return targets


def _decode(raw: bytes) -> str:
    """Decode the upload as UTF-8 (BOM-tolerant). A non-UTF-8 file is a user error, not a 500."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"CSV must be UTF-8 encoded: {exc}") from exc
