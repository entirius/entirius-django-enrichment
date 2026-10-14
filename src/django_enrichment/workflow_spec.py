# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The shape of a workflow definition (contract A4 § 6) — what `WorkflowDefinition.clean()` validates.

A step is `{key, label, template, needs, gate, output}`. `needs` name earlier steps only, so a definition
is acyclic by construction. The step's template must exist and run the kind its output needs
(`STEP_OUTPUT_KINDS`) on an active internal worker; a `document` step must write to the definition's
`target_module`, any other step to the built-in `enrichment` adapter; a `questions`
step always waits for the operator (its answers are the point).
"""

from typing import TYPE_CHECKING, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from django_enrichment.adapters.artifact import KEY as ARTIFACT_KEY
from django_enrichment.enums import STEP_OUTPUT_KINDS, StepGate, StepOutput, WorkerMode

if TYPE_CHECKING:
    from django_enrichment.models import Worker

STEP_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=STEP_KEY_PATTERN, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    template: str = Field(min_length=1, max_length=64)
    needs: list[str] = Field(default_factory=list)
    gate: Literal["review", "auto"] = StepGate.REVIEW.value
    output: Literal["artifact", "questions", "document"] = StepOutput.ARTIFACT.value


_STEPS = TypeAdapter(list[StepSpec])


def parse_steps(steps: object) -> list[StepSpec]:
    """Typed steps. Raises `ValueError` (pydantic's) for a malformed list."""
    return _STEPS.validate_python(steps)


def definition_errors(*, steps: object, inputs_schema: object, target_module: str) -> dict[str, list[str]]:
    """Field → messages for everything wrong with a definition (empty = valid)."""
    errors = {"inputs_schema": inputs_schema_errors(inputs_schema), "steps": step_errors(steps, target_module)}
    return {field: messages for field, messages in errors.items() if messages}


def inputs_schema_errors(schema: object) -> list[str]:
    if not isinstance(schema, dict):
        return ["must be a JSON Schema object"]
    if not schema:
        return []
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"invalid JSON Schema: {exc.message}"]
    return [] if schema.get("type") == "object" else ['the inputs schema must have "type": "object"']


def step_errors(steps: object, target_module: str) -> list[str]:
    try:
        specs = parse_steps(steps)
    except PydanticValidationError as exc:
        return [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
    if not specs:
        return ["a workflow needs at least one step"]
    return _graph_errors(specs) + [msg for spec in specs for msg in _template_errors(spec, target_module)]


def _graph_errors(specs: list[StepSpec]) -> list[str]:
    errors, seen = [], set()
    for spec in specs:
        if spec.key in seen:
            errors.append(f"step {spec.key!r}: duplicate key")
        errors += [
            f"step {spec.key!r}: needs {need!r}, which is not an earlier step"
            for need in spec.needs
            if need not in seen
        ]
        if spec.output == StepOutput.QUESTIONS.value and spec.gate != StepGate.REVIEW.value:
            errors.append(f"step {spec.key!r}: a questions step needs the review gate")
        seen.add(spec.key)
    return errors


def _template_errors(spec: StepSpec, target_module: str) -> list[str]:
    from django_enrichment.models import TaskTemplate

    template = TaskTemplate.objects.select_related("worker").filter(key=spec.template).first()
    if template is None:
        return [f"step {spec.key!r}: unknown template {spec.template!r}"]
    return _worker_errors(spec, template.worker, target_module)


def _worker_errors(spec: StepSpec, worker: "Worker", target_module: str) -> list[str]:
    kind = STEP_OUTPUT_KINDS[spec.output]
    if not worker.active or worker.mode != WorkerMode.INTERNAL.value:
        return [f"step {spec.key!r}: worker {worker.key!r} must be active and internal (our runner runs the steps)"]
    if worker.kind != kind:
        return [f"step {spec.key!r}: output {spec.output!r} needs a {kind!r} worker, template runs {worker.kind!r}"]
    if spec.output == StepOutput.DOCUMENT.value and worker.target_module != target_module:
        return [f"step {spec.key!r}: the document must go to {target_module!r}, not {worker.target_module!r}"]
    if spec.output != StepOutput.DOCUMENT.value and worker.target_module != ARTIFACT_KEY:
        return [
            f"step {spec.key!r}: an {spec.output!r} step must write to the {ARTIFACT_KEY!r} adapter, "
            f"template writes to {worker.target_module!r}"
        ]
    return []
