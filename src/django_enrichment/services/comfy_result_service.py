# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""`ComfyResult` reads and writes, the workflow file and run hash the results are keyed by, and the two proposal
lookups the `comfy` kind needs — shared by the kind and its spawn. The only writer of `ComfyResult`."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from django_enrichment.enums import ProposalStatus
from django_enrichment.kinds.base import SubjectError
from django_enrichment.models import ComfyResult, ContentProposal, compute_locator_hash

WORKFLOW_MISSING = "COMFY_WORKFLOW_MISSING"
WORKFLOW_INVALID = "COMFY_WORKFLOW_INVALID"


@dataclass(frozen=True)
class ResultKey:
    subject_ref: str
    input_sha1: str
    worker_key: str
    run_hash: str


def load_workflow(path: str, shaping: dict) -> tuple[dict, str]:
    """The workflow (ComfyUI API format) and the run hash: sha256 of the file and of `shaping` (the options that
    shape a result). A relative path is read from the host's `BASE_DIR` (the workflow ships in the host's package)."""
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
    return workflow, hashlib.sha256(raw + json.dumps(shaping, sort_keys=True).encode()).hexdigest()


def find(key: ResultKey) -> ComfyResult | None:
    return ComfyResult.objects.filter(**_fields(key)).first()


def save(key: ResultKey, **values) -> ComfyResult:
    """Create or update the row of `key`; `values` = `source`, `metrics`, `verdict`, `reason`, `worker_version`, `task`
    (the task that judged it last)."""
    result, _ = ComfyResult.objects.update_or_create(**_fields(key), defaults=values)
    return result


def checked(*, worker_key: str, run_hash: str, worker_version: int, sha1s: set[str]) -> set[tuple[str, str]]:
    """The `(subject_ref, input_sha1)` pairs among `sha1s` that have a result under this run hash and worker version."""
    rows = ComfyResult.objects.filter(
        worker_key=worker_key, run_hash=run_hash, worker_version=worker_version, input_sha1__in=sha1s
    )
    return set(rows.values_list("subject_ref", "input_sha1"))


def proposal_exists(target_module: str, subject_ref: str, target_kind: str, locator: dict) -> bool:
    """Any proposal, in any status, for this target — a picture the kind proposed landed."""
    return _target(target_module, subject_ref, target_kind, locator).exists()


def last_applied(target_module: str, subject_ref: str, target_kind: str, locator: dict) -> ContentProposal | None:
    """The proposal last applied to this target — who set the value it holds."""
    applied = _target(target_module, subject_ref, target_kind, locator).filter(status=ProposalStatus.APPLIED.value)
    return applied.order_by("-applied_at", "-pk").first()


def _target(target_module: str, subject_ref: str, target_kind: str, locator: dict):
    return ContentProposal.objects.filter(
        target_module=target_module,
        subject_ref=subject_ref,
        target_kind=target_kind,
        locator_hash=compute_locator_hash(locator),
    )


def _fields(key: ResultKey) -> dict:
    return {
        "subject_ref": key.subject_ref,
        "input_sha1": key.input_sha1,
        "worker_key": key.worker_key,
        "run_hash": key.run_hash,
    }
