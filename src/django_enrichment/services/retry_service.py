# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retry a finished task: a new task for the subjects that failed, spawned the way `POST tasks/` spawns one.

Same type, template and operator params as the original; the keys the bus wrote into the original's params while
it ran (`RUNTIME_PARAMS`) stay behind and `params.retry_of` names the original. The scope is the distinct
`subject_ref`s of the original's `errors` as a list scope; a failed task whose errors name no subject (the whole run
failed) retries its original scope. A workflow step is not retried here — it has its own regenerate.
"""

from django.contrib.auth.models import AbstractBaseUser

from django_enrichment.enums import TASK_STATUS_TRANSITIONS, TaskStatus
from django_enrichment.models import EnrichmentTask
from django_enrichment.schemas.requests import TaskSpawnRequest
from django_enrichment.services import run_service, workflow_steps
from django_enrichment.services.qa_service import CONTEXT_NUMBERS
from django_enrichment.services.task_service import RETRY_OF

# Written into `task.params` after creation, per run: the QA number context of the subjects it ran and its own marker.
RUNTIME_PARAMS = frozenset({CONTEXT_NUMBERS, RETRY_OF})


class NotRetryableError(Exception):
    """The task is still running, or finished without a failed subject: nothing to retry (a state conflict)."""


def retry(task: EnrichmentTask, *, user: AbstractBaseUser | None = None) -> run_service.SpawnResult:
    """Spawn the retry of `task` as `user`'s run. Raises `NotRetryableError`, `run_service.AlreadyRunningError`,
    `ValueError` (a workflow step, or the spawn refused the run like `POST tasks/` would)."""
    if _is_workflow_step(task):
        raise ValueError("This run is a step of a workflow run. Regenerate the step in its run instead.")
    if not _is_finished(task):
        raise NotRetryableError("This run is still in progress. Wait for it to finish, then retry.")
    order = TaskSpawnRequest(
        type=task.type,
        scope_spec=_retry_scope(task),
        params={key: value for key, value in task.params.items() if key not in RUNTIME_PARAMS},
        template=task.params.get("template", ""),
    )
    return run_service.spawn(order, requested_by=user, retry_of=task.pk)


def _is_finished(task: EnrichmentTask) -> bool:
    """Done, failed or cancelled: a status the state machine has no way out of."""
    return not TASK_STATUS_TRANSITIONS[task.status]


def _failed_refs(task: EnrichmentTask) -> list[str]:
    """Distinct subjects named by the task's errors, in the order they failed (a run-level error names none)."""
    return list(dict.fromkeys(error["subject_ref"] for error in task.errors if error.get("subject_ref")))


def _retry_scope(task: EnrichmentTask) -> dict:
    if refs := _failed_refs(task):
        return {**_list_scope(task.scope_spec), "refs": refs}
    if task.status == TaskStatus.FAILED.value:
        return task.scope_spec
    raise NotRetryableError("Nothing failed in this run, so there is nothing to retry.")


def _list_scope(scope_spec: dict) -> dict:
    """A list scope keeps its keys; any other scope becomes a list of the same module (and channel)."""
    if scope_spec.get("mode") == "list":
        return scope_spec
    return {"mode": "list", **{key: scope_spec[key] for key in ("module", "channel") if key in scope_spec}}


def _is_workflow_step(task: EnrichmentTask) -> bool:
    refs = [*task.scope_spec.get("refs", []), *(error.get("subject_ref", "") for error in task.errors)]
    return bool(task.params.get("run")) or any(workflow_steps.run_uid(str(ref)) is not None for ref in refs)
