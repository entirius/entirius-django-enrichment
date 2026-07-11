# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Staging-store — the bus's private binary store for media proposals (etap-08).

A media proposal's binary (a generated image from the worker) is held HERE, never in the
target module, until an operator accepts. Reject deletes it → the source module (PIM) has zero
trace; accept hands it to the adapter, which uploads it to PIM and then the bus drops the staged
copy (the old `Picture`, not this file, is the undo anchor).

Filesystem backend behind a thin function interface — `save`/`open_file`/`delete`/`exists`. The
interface is the stable seam: an S3/object-store backend can replace the body later without
touching callers (recorded as a future finding, not built now). The root dir is read at CALL
time via `getattr(settings, "ENRICHMENT_STAGING_DIR", ...)` (mirrors the adapter registry: a
constant snapshotted at import would ignore a host override or a test `settings` fixture).

A `ref` is an opaque flat filename (`<uuid>.<ext>`). Callers persist it in
`ContentProposal.staged_file` and never interpret it. `open_file`/`delete`/`exists` reject any
ref containing a path separator or `..` so a crafted ref can't escape the root (traversal guard).
"""

import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files import File


def _root() -> Path:
    """Resolve (and create) the staging root. Defaults under MEDIA_ROOT, else the system tempdir."""
    configured = getattr(settings, "ENRICHMENT_STAGING_DIR", None)
    if not configured:
        media_root = getattr(settings, "MEDIA_ROOT", "") or ""
        base = Path(media_root) if media_root else Path(tempfile.gettempdir())
        configured = base / "enrichment_staging"
    root = Path(configured)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_path(ref: str) -> Path:
    """Map a ref to an absolute path inside the root, refusing anything that could escape it."""
    if not ref or "/" in ref or "\\" in ref or ".." in ref:
        raise ValueError(f"unsafe staging ref: {ref!r}")
    return _root() / ref


def save(file_obj) -> str:
    """Persist an uploaded file and return its opaque ref. Extension preserved for content sniffing."""
    suffix = Path(getattr(file_obj, "name", "") or "").suffix
    ref = f"{uuid.uuid4().hex}{suffix}"
    dest = _safe_path(ref)
    file_obj.seek(0)
    with dest.open("wb") as out:
        for chunk in file_obj.chunks():
            out.write(chunk)
    return ref


def open_file(ref: str) -> File:
    """Return a readable Django `File` for `ref`, named by its basename (so downstream upload
    helpers that build temp names from `.name` get a clean filename, not a path)."""
    return File(_safe_path(ref).open("rb"), name=ref)


def delete(ref: str) -> None:
    """Remove the staged file. Idempotent — a missing ref is a no-op (cleanup must never raise)."""
    if not ref:
        return
    path = _safe_path(ref)
    if path.exists():
        path.unlink()


def exists(ref: str) -> bool:
    if not ref:
        return False
    return _safe_path(ref).exists()
