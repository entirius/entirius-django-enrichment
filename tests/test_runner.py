# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Internal runner: dispatch on commit, open → in_progress → done/failed, per-subject isolation.

A test kind (`echo`) stands in for the AI kinds, so these tests exercise the runner and intake
without the toolbox; the toolbox-backed kinds are covered in `test_kinds.py`.
"""

import json

import pytest
from django_utils.toolbox import ToolboxError
from pydantic import BaseModel

from django_enrichment.enums import RevisionAuthor, TaskStatus
from django_enrichment.kinds import Kind, ProposalDraft, registry
from django_enrichment.models import ContentProposal
from django_enrichment.services import revision_service, task_service, worker_service
from tests.queue_helpers import run_queue


class EchoOptions(BaseModel):
    pass


class EchoParams(BaseModel):
    channel: str = "default"
    fail_on: list[str] = []
    toolbox_fail_on: list[str] = []
    cancel_on: list[str] = []


class EchoKind(Kind):
    Options = EchoOptions
    Params = EchoParams

    def run(self, worker, task, subject_ref):
        params = EchoParams.model_validate(task.params)
        if subject_ref in params.fail_on:
            raise RuntimeError("boom")
        if subject_ref in params.toolbox_fail_on:
            raise ToolboxError(400, "secret upstream text", code="TRANSLATOR_NOT_CONFIGURED")
        if subject_ref in params.cancel_on:  # operator cancels mid-run through a fresh object
            task_service.transition_status(task_service.get_task(task.pk), TaskStatus.CANCELLED.value)
        locator = {"channel": params.channel, "language": "pl", "feature_idx": "description"}
        return [ProposalDraft("attribute_value", locator, {"text": f"new {subject_ref}"}, subject_label=subject_ref)]


def _nameless_run(self, worker, task, subject_ref):
    """A kind run whose draft names no subject (the write and pick kinds)."""
    return [ProposalDraft("attribute_value", {"language": "pl"}, {"text": subject_ref})]


@pytest.fixture
def echo_worker(make_worker, fake_adapter, monkeypatch):
    monkeypatch.setitem(registry._KINDS, "echo", EchoKind)

    def _make(**over):
        return make_worker(**{"key": "pim.echo", "kind": "echo", "options": {}, **over})

    return _make


def _spawn(refs, mode="list", **params):
    scope_spec = {"mode": mode, "module": "pim", "refs": refs}
    return task_service.spawn(type="pim.echo", scope_spec=scope_spec, params=params)


@pytest.mark.django_db
class TestDispatch:
    def test_internal_worker_runs_after_commit(self, echo_worker, django_capture_on_commit_callbacks):
        echo_worker()
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            task = _spawn(["SKU-1", "SKU-2"])
        task.refresh_from_db()
        assert len(callbacks) == 2  # plan, then the consumer it starts
        assert task.status == TaskStatus.DONE.value
        assert task.counts == {"proposed": 2}
        proposal = ContentProposal.objects.get(subject_ref="SKU-1")
        assert proposal.source == "worker:pim.echo@v1"
        assert proposal.task_id == task.pk
        assert proposal.subject_label == "SKU-1"

    def test_inactive_worker_is_not_dispatched(self, echo_worker, django_capture_on_commit_callbacks):
        echo_worker(active=False)
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            task = _spawn(["SKU-1"])
        assert callbacks == []
        task.refresh_from_db()
        assert task.status == TaskStatus.OPEN.value

    def test_external_worker_gets_units_but_no_consumer(self, echo_worker, django_capture_on_commit_callbacks):
        echo_worker(mode="external")
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            task = _spawn(["SKU-1"])
        assert len(callbacks) == 1  # plan only
        assert task.units.filter(status="queued").count() == 1
        task.refresh_from_db()
        assert task.status == TaskStatus.OPEN.value

    def test_task_without_worker_is_not_dispatched(self, fake_adapter, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            task_service.spawn(type="fix-attribute", scope_spec={"mode": "list", "module": "pim", "refs": ["SKU-1"]})
        assert callbacks == []


@pytest.mark.django_db
class TestRun:
    def _run(self, refs, mode="list", **params):
        task = _spawn(refs, mode=mode, **params)  # on_commit is not executed inside the test transaction
        run_queue(task)
        task.refresh_from_db()
        return task

    def test_failing_subject_is_counted_and_task_continues(self, echo_worker):
        echo_worker()
        task = self._run(["SKU-1", "SKU-2", "SKU-3"], fail_on=["SKU-2"])
        assert task.status == TaskStatus.DONE.value
        assert task.counts == {"proposed": 2, "failed": 1}

    def test_all_subjects_failed_fails_the_task(self, echo_worker):
        echo_worker()
        task = self._run(["SKU-1", "SKU-2"], fail_on=["SKU-1", "SKU-2"])
        assert task.status == TaskStatus.FAILED.value
        assert task.counts == {"failed": 2}

    def test_no_targets_is_done(self, echo_worker):
        echo_worker()
        assert self._run([]).status == TaskStatus.DONE.value

    def test_version_is_stamped_into_source(self, echo_worker):
        worker = echo_worker()
        worker.system_prompt = "Recalibrated."
        worker_service.save_worker(worker)
        self._run(["SKU-1"])
        assert ContentProposal.objects.get().source == "worker:pim.echo@v2"

    def test_gap_mode_ends_when_only_failed_subjects_remain(self, echo_worker):
        echo_worker()
        candidates = [{"subject_ref": "SKU-1", "target_kind": "attribute_value"}, {"subject_ref": "SKU-2"}]
        task = task_service.spawn(
            type="pim.echo",
            scope_spec={"mode": "gap", "module": "pim", "check": "c", "scope": {"candidates": candidates}},
            params={"fail_on": ["SKU-2"]},
        )
        run_queue(task)
        task.refresh_from_db()
        assert task.status == TaskStatus.DONE.value
        assert task.counts == {"proposed": 1, "failed": 1}

    def test_resolve_failure_fails_the_task(self, echo_worker):
        echo_worker()
        task = _spawn(["SKU-1"])
        task.scope_spec = {"mode": "list", "module": "unknown", "refs": ["SKU-1"]}
        task.save()
        run_queue(task)
        task.refresh_from_db()
        assert task.status == TaskStatus.FAILED.value

    def test_cancel_mid_run_stops_and_stays_cancelled(self, echo_worker):
        echo_worker()
        task = self._run(["SKU-1", "SKU-2", "SKU-3"], cancel_on=["SKU-1"])
        assert task.status == TaskStatus.CANCELLED.value
        assert not ContentProposal.objects.exists()  # cancelled before submit → the unit's results are discarded
        assert set(task.units.values_list("status", flat=True)) == {"cancelled"}

    def test_rerun_supersedes_an_untouched_pending_proposal(self, echo_worker):
        echo_worker()
        self._run(["SKU-1"])
        task = self._run(["SKU-1"])
        statuses = sorted(ContentProposal.objects.values_list("status", flat=True))
        assert statuses == ["pending", "superseded"]
        assert task.counts == {"proposed": 1}

    def test_rerun_keeps_a_pending_proposal_the_operator_edited(self, echo_worker):
        echo_worker()
        self._run(["SKU-1", "SKU-2"])
        edited = ContentProposal.objects.get(subject_ref="SKU-1")
        revision_service.add_revision(edited, {"text": "mine"}, author_kind=RevisionAuthor.USER.value)
        task = self._run(["SKU-1", "SKU-2"])
        edited.refresh_from_db()
        assert (edited.status, edited.proposed_value) == ("pending", {"text": "mine"})
        assert ContentProposal.objects.filter(subject_ref="SKU-1").count() == 1
        assert task.counts == {"proposed": 1, "kept_edited": 1}

    def test_a_nameless_draft_gets_the_subject_name(self, echo_worker, fake_adapter, monkeypatch):
        echo_worker()
        fake_adapter.set_context({"name": "Orion lamp"})
        monkeypatch.setattr(EchoKind, "run", _nameless_run)
        self._run(["SKU-1"])
        assert ContentProposal.objects.get().subject_label == "Orion lamp"

    def test_a_failing_name_lookup_leaves_the_label_empty(self, echo_worker, fake_adapter, monkeypatch):
        echo_worker()
        monkeypatch.setattr(fake_adapter, "build_context", lambda **_: 1 / 0)
        monkeypatch.setattr(EchoKind, "run", _nameless_run)
        task = self._run(["SKU-1"])
        assert (task.status, ContentProposal.objects.get().subject_label) == (TaskStatus.DONE.value, "")

    def test_task_not_open_is_skipped(self, echo_worker):
        echo_worker()
        task = _spawn(["SKU-1"])
        task_service.mark_in_progress(task)
        run_queue(task)
        task.refresh_from_db()
        assert task.status == TaskStatus.IN_PROGRESS.value
        assert not ContentProposal.objects.exists()


@pytest.mark.django_db
class TestFailureReasons:
    def _run(self, refs, **params):
        task = _spawn(refs, **params)
        run_queue(task)
        task.refresh_from_db()
        return task

    def test_failed_subject_records_a_code_and_error_id_never_the_text(self, echo_worker):
        echo_worker()
        task = self._run(["SKU-1", "SKU-2"], fail_on=["SKU-2"])
        [error] = task.errors
        assert error["subject_ref"] == "SKU-2"
        assert error["code"] == "INTERNAL_ERROR"
        assert len(error["error_id"]) == 12
        assert "boom" not in json.dumps(task.errors)

    def test_toolbox_error_keeps_its_code(self, echo_worker):
        echo_worker()
        task = self._run(["SKU-1"], toolbox_fail_on=["SKU-1"])
        assert task.status == TaskStatus.FAILED.value
        assert [e["code"] for e in task.errors] == ["TRANSLATOR_NOT_CONFIGURED"]
        assert "secret" not in json.dumps(task.errors)

    def test_resolve_failure_is_recorded_without_a_subject(self, echo_worker):
        echo_worker()
        task = _spawn(["SKU-1"])
        task.scope_spec = {"mode": "list", "module": "unknown", "refs": ["SKU-1"]}
        task.save()
        run_queue(task)
        task.refresh_from_db()
        assert [(e["subject_ref"], e["code"]) for e in task.errors] == [("", "INTERNAL_ERROR")]
