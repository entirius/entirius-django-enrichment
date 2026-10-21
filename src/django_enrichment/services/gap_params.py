# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Bus-level params of a gap-mode run (a leaf: no service imports).

The gap resolve reads the budget (`limit`, `cooldown_days`) from `task.params`, and a spawn rule's task records the
rule's key there. A kind's `Params` drop the keys they do not declare, so `task_service.resolve_type_and_params`
carries these past that validation with `carried_params` (the same contract as the augmentation and QA keys).
"""

from pydantic import BaseModel, Field, ValidationError

LIMIT = "limit"
COOLDOWN_DAYS = "cooldown_days"
SPAWN_RULE = "spawn_rule"


class GapParams(BaseModel):
    """The gap keys of a task's params. Every one is optional: only the given ones are carried (`exclude_unset`),
    the module settings fill what is absent."""

    limit: int | None = Field(None, ge=0, description="Most targets the task feeds the worker (null = no cap)")
    cooldown_days: int = Field(0, ge=0, description="Days a rejected or applied target stays out of new tasks")
    spawn_rule: str = Field("", max_length=64, description="Key of the spawn rule that created the task")


def carried_params(params: dict) -> dict:
    """The gap keys of `params`, validated (only those given). Raises `ValueError`."""
    try:
        return GapParams.model_validate(params).model_dump(exclude_unset=True)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"params invalid: {details}") from exc
