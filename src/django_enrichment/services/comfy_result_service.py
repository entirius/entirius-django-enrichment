# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""`ComfyResult` reads and writes, and the workflow file the results are keyed by — shared by the `comfy` kind and its
spawn. The only writer of `ComfyResult`."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from django_enrichment.kinds.base import SubjectError
from django_enrichment.models import ComfyResult

WORKFLOW_MISSING = "COMFY_WORKFLOW_MISSING"
WORKFLOW_INVALID = "COMFY_WORKFLOW_INVALID"


@dataclass(frozen=True)
class ResultKey:
    subject_ref: str
    input_sha1: str
    worker_key: str
    workflow_hash: str


def load_workflow(path: str) -> tuple[dict, str]:
    """The workflow (ComfyUI API format) and its version, the sha256 of the file. A relative path is read from the
    host's `BASE_DIR` (the workflow ships in the host's own package)."""
    file = Path(path)
    if not file.is_absolute():
        file = Path(getattr(settings, "BASE_DIR", ".")) / file
    try:
        raw = file.read_bytes()
    except OSError as exc:
        raise SubjectError(WORKFLOW_MISSING, f"workflow file {path!r} cannot be read") from exc
    try:
        workflow = json.loads(raw)
    except ValueError as exc:
        raise SubjectError(WORKFLOW_INVALID, f"workflow file {path!r} is not JSON") from exc
    if not isinstance(workflow, dict):
        raise SubjectError(WORKFLOW_INVALID, f"workflow file {path!r} is not a ComfyUI API workflow")
    return workflow, hashlib.sha256(raw).hexdigest()


def find(key: ResultKey) -> ComfyResult | None:
    return ComfyResult.objects.filter(**_fields(key)).first()


def find_same_file(key: ResultKey) -> ComfyResult | None:
    """A result of the same file under the same workflow on another product — its metrics need no second run."""
    file = {k: v for k, v in _fields(key).items() if k != "subject_ref"}
    return ComfyResult.objects.filter(**file).exclude(subject_ref=key.subject_ref).order_by("pk").first()


def save(key: ResultKey, **values) -> ComfyResult:
    """Create or update the row of `key`; `values` = `source`, `metrics`, `verdict`, `reason`, `worker_version`."""
    result, _ = ComfyResult.objects.update_or_create(**_fields(key), defaults=values)
    return result


def checked(*, worker_key: str, workflow_hash: str, worker_version: int, sha1s: set[str]) -> set[tuple[str, str]]:
    """The `(subject_ref, input_sha1)` pairs among `sha1s` that have a result under this workflow file and this worker
    version."""
    rows = ComfyResult.objects.filter(
        worker_key=worker_key, workflow_hash=workflow_hash, worker_version=worker_version, input_sha1__in=sha1s
    )
    return set(rows.values_list("subject_ref", "input_sha1"))


def _fields(key: ResultKey) -> dict:
    return {
        "subject_ref": key.subject_ref,
        "input_sha1": key.input_sha1,
        "worker_key": key.worker_key,
        "workflow_hash": key.workflow_hash,
    }
