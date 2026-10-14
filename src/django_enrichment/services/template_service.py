# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Task templates (contract A3 § 4) — CRUD, validation and the merge into a spawned task.

A template presets a worker's `params` and `rules`. `POST tasks/` with `template` resolves to the
template's worker (`type`), merges params (the request wins), concatenates rules (template first)
and keeps the key in `params.template`, so corrections can find the template's rules later.
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import QuerySet

from django_enrichment.models import TaskTemplate, Worker
from django_enrichment.services import rules_service

EDITABLE_FIELDS = frozenset({"label", "worker_key", "params", "rules", "position", "active"})


def list_templates(target_module: str | None = None) -> QuerySet[TaskTemplate]:
    qs = TaskTemplate.objects.select_related("worker")
    if target_module:
        qs = qs.filter(worker__target_module=target_module)
    return qs.order_by("position", "key")


def get_template(key: str) -> TaskTemplate:
    """Raises `ValueError` ("... not found") for an unknown key."""
    if not (template := list_templates().filter(key=key).first()):
        raise ValueError(f"template {key!r} not found")
    return template


def find(key: str) -> TaskTemplate | None:
    return TaskTemplate.objects.select_related("worker").filter(key=key).first() if key else None


def create_template(*, key: str, worker_key: str, **fields: Any) -> TaskTemplate:
    if TaskTemplate.objects.filter(key=key).exists():
        raise ValueError(f"template {key!r} already exists")
    return update_template(TaskTemplate(key=key), {"worker_key": worker_key, **fields})


def update_template(template: TaskTemplate, updates: dict[str, Any]) -> TaskTemplate:
    """Apply whitelisted `updates` (`worker_key` → the worker FK), validate, save. Raises `ValueError`."""
    if invalid := set(updates) - EDITABLE_FIELDS:
        raise ValueError(f"fields not editable on a template: {sorted(invalid)}")
    for name, value in updates.items():
        if name == "worker_key":
            template.worker = _worker(value)
        else:
            setattr(template, name, value)
    return save_template(template)


def save_template(template: TaskTemplate) -> TaskTemplate:
    """Rule limits + `full_clean` (params against the worker kind), then save. Raises `ValueError`."""
    template.rules = rules_service.validate_rules(list(template.rules or []))
    try:
        template.full_clean()
    except ValidationError as exc:
        raise ValueError(_message(exc)) from exc
    template.save()
    return template


def delete_template(template: TaskTemplate) -> None:
    template.delete()


def describe(template: TaskTemplate) -> dict[str, Any]:
    worker = template.worker
    return {
        "key": template.key,
        "label": template.label,
        "worker_key": worker.key,
        "worker_label": worker.label,
        "kind": worker.kind,
        "target_module": worker.target_module,
        "params": template.params,
        "rules": template.rules,
        "position": template.position,
        "active": template.active,
    }


def merge_into_task(key: str, *, task_type: str, params: dict) -> tuple[str, dict]:
    """`(type, params)` of a task spawned from template `key`. Raises `ValueError` (unknown/inactive/mismatch)."""
    template = find(key)
    if template is None or not template.active:
        raise ValueError(f"unknown or inactive template {key!r}")
    worker = template.worker
    if not worker.active:
        raise ValueError(f"the worker of template {key!r} is inactive")
    if task_type and task_type != worker.key:
        raise ValueError(f"template {key!r} runs worker {worker.key!r}, not {task_type!r}")
    rules = [*template.rules, *template.params.get("rules", []), *params.get("rules", [])]
    return worker.key, {**template.params, **params, "rules": list(dict.fromkeys(rules))}


def add_rule(template: TaskTemplate, rule: str) -> list[str]:
    template.rules = list(dict.fromkeys([*template.rules, rule]))
    save_template(template)
    return template.rules


def _worker(key: str) -> Worker:
    if not (worker := Worker.objects.filter(key=key).first()):
        raise ValueError(f"unknown worker {key!r}")
    return worker


def _message(exc: ValidationError) -> str:
    if not hasattr(exc, "message_dict"):
        return "; ".join(exc.messages)
    return "; ".join(f"{field}: {msg}" for field, msgs in exc.message_dict.items() for msg in msgs)
