# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Workflow reads — definitions and runs as the API shows them (commands live in `workflow_service`)."""

from typing import Any

from django.db.models import QuerySet

from django_enrichment.models import WorkflowDefinition, WorkflowRun
from django_enrichment.services import workflow_steps
from django_enrichment.workflow_spec import parse_steps


def list_definitions() -> QuerySet[WorkflowDefinition]:
    return WorkflowDefinition.objects.filter(active=True).order_by("key")


def describe_definition(definition: WorkflowDefinition) -> dict[str, Any]:
    return {
        "key": definition.key,
        "label": definition.label,
        "target_module": definition.target_module,
        "inputs_schema": definition.inputs_schema,
        "steps": [spec.model_dump() for spec in parse_steps(definition.steps)],
    }


def list_runs(*, status: str | None = None, definition: str | None = None) -> QuerySet[WorkflowRun]:
    qs = WorkflowRun.objects.select_related("definition")
    if status:
        qs = qs.filter(status=status)
    if definition:
        qs = qs.filter(definition__key=definition)
    return qs.order_by("-created_at")


def get_run(uid: str) -> WorkflowRun:
    """Raises `ValueError(... not found)` for an unknown uid."""
    if run := WorkflowRun.objects.select_related("definition").filter(uid=uid).first():
        return run
    raise ValueError(f"workflow run {uid!r} not found")


def describe(run: WorkflowRun) -> dict[str, Any]:
    """The API view of a run: identity, brief, steps with their state, cost."""
    return {
        "uid": run.uid,
        "definition": run.definition.key,
        "definition_label": run.definition.label,
        "channel": run.channel,
        "inputs": run.inputs,
        "subject_ref": run.subject_ref,
        "status": run.status,
        "current_step": workflow_steps.current_step(run),
        "steps": run.steps_state,
        "cost_usd": run.cost_usd,
        "cost_complete": run.cost_complete,
        "created_by_id": run.created_by_id,
        "created_at": run.created_at,
        "modified_at": run.modified_at,
    }
