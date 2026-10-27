# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""What a claimed unit looks like on the wire (worker API) — no secrets, no subject content."""

from django_enrichment.models import Worker, WorkUnit


def describe(unit: WorkUnit) -> dict:
    task = unit.task
    worker = Worker.objects.get(key=unit.worker_key)
    return {
        "unit_id": unit.pk,
        "token": str(unit.lease_token),
        "lease_until": unit.lease_until,
        "attempt": unit.attempts,
        "task_id": task.pk,
        "type": task.type,
        "params": task.params,
        "scope_module": task.scope_spec.get("module", ""),
        "subject_refs": unit.subject_refs,
        "context": {
            "kind": worker.kind,
            "version": worker.version,
            "system_prompt": worker.system_prompt,
            "model": worker.model,
            "options": worker.options,
        },
    }
