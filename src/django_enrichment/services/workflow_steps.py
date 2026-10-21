# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""A run's `steps_state` — the frozen steps with their state — and how a step is spawned.

Only `workflow_service` calls this, always under the run's row lock. A step is spawned as an ordinary task
of its template, on the run's subject `run:<uid>` (resolved by the built-in `enrichment` adapter), with
frozen params: run, step, channel, the brief, the approved values of the steps it needs and — for a
regeneration — the previous value and the operator's instruction. A brief field whose inputs-schema property lists
`x-steps` reaches only those steps (the cover's hints stay out of the research and the writing).
"""

from decimal import Decimal

from django_enrichment.adapters.artifact import KEY as ARTIFACT_MODULE
from django_enrichment.enums import RunStatus, StepStatus, TaskLane
from django_enrichment.models import ContentProposal, EnrichmentTask, WorkflowDefinition, WorkflowRun
from django_enrichment.services import task_service
from django_enrichment.workflow_spec import parse_steps

RUN_PREFIX = "run:"
STEPS_KEYWORD = "x-steps"


def subject_ref(run: WorkflowRun) -> str:
    return f"{RUN_PREFIX}{run.uid}"


def run_uid(ref: str) -> str | None:
    """The run uid of a step proposal's subject (`run:<uid>`), None for any other subject."""
    return ref[len(RUN_PREFIX) :] if ref.startswith(RUN_PREFIX) else None


def initial_state(definition: WorkflowDefinition) -> list[dict]:
    """The definition's steps, frozen for one run, all waiting."""
    state = {"status": StepStatus.WAITING.value, "task_id": None, "proposal_id": None}
    return [{**spec.model_dump(), **state} for spec in parse_steps(definition.steps)]


def find(run: WorkflowRun, key: str) -> dict:
    """The step `key` of the run. Raises `ValueError(... not found)`."""
    if step := next((step for step in run.steps_state if step["key"] == key), None):
        return step
    raise ValueError(f"step {key!r} not found")


def ready(run: WorkflowRun) -> list[dict]:
    """Waiting steps whose needed steps are all approved."""
    approved = {step["key"] for step in run.steps_state if step["status"] == StepStatus.APPROVED.value}
    waiting = [step for step in run.steps_state if step["status"] == StepStatus.WAITING.value]
    return [step for step in waiting if set(step["needs"]) <= approved]


def spawn(run: WorkflowRun, step: dict, *, instruction: str = "", previous: dict | None = None) -> None:
    """Spawn the step's task (frozen params) and mark the step running."""
    params = {
        "run": run.uid,
        "step": step["key"],
        "channel": run.channel,
        "brief": brief_for(run, step["key"]),
        "inputs": _inputs(run, step),
        "instruction": instruction,
        "previous": previous,
    }
    scope_spec = {"mode": "list", "module": ARTIFACT_MODULE, "refs": [subject_ref(run)]}
    task = task_service.spawn(
        type="",
        template=step["template"],
        scope_spec=scope_spec,
        params=params,
        requested_by=run.created_by,
        lane=TaskLane.NOW.value,
    )
    step.update(status=StepStatus.RUNNING.value, task_id=task.pk, proposal_id=None)


def brief_for(run: WorkflowRun, key: str) -> dict:
    """The run's brief without the fields meant for other steps (`x-steps`)."""
    properties = (run.definition.inputs_schema or {}).get("properties") or {}
    hidden = {
        name
        for name, prop in properties.items()
        if isinstance(prop, dict) and STEPS_KEYWORD in prop and key not in (prop[STEPS_KEYWORD] or [])
    }
    return {name: value for name, value in (run.inputs or {}).items() if name not in hidden}


def _inputs(run: WorkflowRun, step: dict) -> dict[str, dict]:
    """`{needed step key: its approved value}`."""
    ids = {item["key"]: item["proposal_id"] for item in run.steps_state if item["key"] in step["needs"]}
    values = dict(ContentProposal.objects.filter(pk__in=ids.values()).values_list("pk", "proposed_value"))
    return {key: values.get(pk) or {} for key, pk in ids.items()}


# First match wins: anything running → running, else a failure → failed, else something to review → waiting.
_STATUS_ORDER = (
    (StepStatus.RUNNING.value, RunStatus.RUNNING.value),
    (StepStatus.FAILED.value, RunStatus.FAILED.value),
    (StepStatus.REVIEW.value, RunStatus.WAITING.value),
)


def derive_status(run: WorkflowRun) -> str:
    """Run status from its steps; a cancelled run stays cancelled, all approved = done."""
    if run.status == RunStatus.CANCELLED.value:
        return run.status
    statuses = {step["status"] for step in run.steps_state}
    if statuses == {StepStatus.APPROVED.value}:
        return RunStatus.DONE.value
    return next((run_status for step_status, run_status in _STATUS_ORDER if step_status in statuses), run.status)


def refresh_cost(run: WorkflowRun) -> None:
    """`cost_usd` = sum of the known costs of every task the run spawned (regenerations included)."""
    rows = list(EnrichmentTask.objects.filter(params__run=run.uid).values_list("cost_usd", "cost_complete"))
    known = [cost for cost, _ in rows if cost is not None]
    run.cost_usd = sum(known, Decimal(0)) if known else None
    run.cost_complete = all(complete for _, complete in rows)


def current_step(run: WorkflowRun) -> str | None:
    """The first step not approved yet (None once the run is done)."""
    return next((step["key"] for step in run.steps_state if step["status"] != StepStatus.APPROVED.value), None)
