# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Workflow runs end to end on a mocked toolbox (contract A4 § 6): gates, regenerate, failure, cancel, costs."""

import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django_utils.toolbox.testing import error_response, mock_toolbox

from django_enrichment.kinds.questions import AnswersMissingError
from django_enrichment.models import ContentProposal, EnrichmentTask
from django_enrichment.services import (
    proposal_service,
    task_service,
    undo_service,
    workflow_queries,
    workflow_service,
)
from tests import workflow_setup as wf

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("toolbox_settings")]


@pytest.fixture
def setup(fake_adapter, settings):
    settings.ENRICHMENT_ADAPTERS = {"pim": "tests.fake_adapter", wf.DOC_MODULE: "tests.fake_adapter"}
    wf.make_templates()
    return fake_adapter


@pytest.fixture
def toolbox():
    with mock_toolbox() as router:
        route = router["complete"].mock(side_effect=wf.respond)
        yield route


@pytest.fixture
def two_steps(setup):
    """outline (auto gate) → draft (review gate)."""
    steps = [wf.step("outline", gate="auto"), wf.step("draft", needs=("outline",))]
    return wf.make_definition("two", steps, {"type": "object", "required": ["topic"]})


@pytest.fixture
def run_steps(django_capture_on_commit_callbacks):
    """`with run_steps():` — spawned step tasks really run (on-commit callbacks executed, Celery eager)."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


def _states(run) -> dict[str, str]:
    return {step["key"]: step["status"] for step in run.steps_state}


def _proposal(run, key: str) -> ContentProposal:
    return ContentProposal.objects.get(pk=next(s["proposal_id"] for s in run.steps_state if s["key"] == key))


def _start(run_steps, admin_user, definition: str = "two"):
    with run_steps():
        run = workflow_service.start(
            definition=definition, inputs={"topic": "Sofa"}, channel="default", user=admin_user
        )
    return workflow_queries.get_run(run.uid)


def _accept(run_steps, proposal, user):
    with run_steps():
        proposal_service.accept(proposal, user=user)
    return workflow_queries.get_run(proposal.subject_ref.removeprefix("run:"))


class TestTwoStepRun:
    def test_auto_gate_approves_and_the_next_step_waits_for_review(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        assert _states(run) == {"outline": "approved", "draft": "review"}
        assert (run.status, workflow_queries.describe(run)["current_step"]) == ("waiting", "draft")
        outline, draft = _proposal(run, "outline"), _proposal(run, "draft")
        assert (outline.status, outline.target_module, outline.subject_ref) == (
            "applied",
            "enrichment",
            f"run:{run.uid}",
        )
        assert (draft.status, draft.target_kind, draft.target_locator) == ("pending", "artifact", {"step": "draft"})
        assert draft.proposed_value == {"text": "# Linen sofa care\n\nBrush it weekly."}

    def test_frozen_params_and_tags(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        task = EnrichmentTask.objects.get(pk=_proposal(run, "draft").task_id)
        assert task.params["inputs"] == {"outline": {"text": "# Linen sofa care\n\nBrush it weekly."}}
        assert (task.params["brief"], task.params["template"], task.requested_by) == (
            {"topic": "Sofa"},
            "draft",
            admin_user,
        )
        body = wf.sent(toolbox)[1]
        assert body["tags"] == ["enrichment.generate.draft", f"task:{task.pk}", f"run:{run.uid}"]
        assert "Write the article." in body["messages"][1]["content"]
        assert "<inputs>" in body["messages"][1]["content"]

    def test_costs_of_every_task_are_summed(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        assert (run.cost_usd, run.cost_complete) == (Decimal("0.02"), True)

    def test_approval_finishes_the_run(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        proposal_service.accept(_proposal(run, "draft"), user=admin_user)
        run = workflow_queries.get_run(run.uid)
        assert (run.status, workflow_queries.describe(run)["current_step"]) == ("done", None)

    def test_regenerate_supersedes_and_passes_the_instruction(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        old = _proposal(run, "draft")
        with run_steps():
            run = workflow_service.regenerate(run.uid, "draft", instruction="Shorter please")
        run = workflow_queries.get_run(run.uid)
        old.refresh_from_db()
        new = _proposal(run, "draft")
        assert (old.status, new.status, _states(run)["draft"]) == ("superseded", "pending", "review")
        prompt = wf.sent(toolbox)[-1]["messages"][1]["content"]
        assert "Shorter please" in prompt and "<previous_version>" in prompt
        assert run.cost_usd == Decimal("0.03")

    def test_approved_step_cannot_be_regenerated_nor_undone(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        with pytest.raises(ValueError, match="approved"):
            workflow_service.regenerate(run.uid, "outline")
        with pytest.raises(ValueError, match="cannot be undone"):
            undo_service.revert(_proposal(run, "outline"))


class TestFailureAndCancel:
    def test_failed_step_fails_the_run_and_can_be_regenerated(self, two_steps, run_steps, admin_user):
        with mock_toolbox() as router:
            router["complete"].mock(return_value=error_response(502, "PROVIDER_ERROR"))
            run = _start(run_steps, admin_user)
        assert (_states(run)["outline"], run.status, run.cost_complete) == ("failed", "failed", False)
        with mock_toolbox() as router:
            router["complete"].mock(side_effect=wf.respond)
            with run_steps():
                workflow_service.regenerate(run.uid, "outline")
        run = workflow_queries.get_run(run.uid)
        assert _states(run) == {"outline": "approved", "draft": "review"}
        assert run.status == "waiting"

    def test_cancel_rejects_pending_output_and_closes_the_run(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        run = workflow_service.cancel(run.uid, user=admin_user)
        assert run.status == "cancelled"
        assert _proposal(run, "draft").status == "rejected"
        with pytest.raises(ValueError, match="cancelled"):
            workflow_service.regenerate(run.uid, "draft")
        with pytest.raises(ValueError, match="cancelled"):
            workflow_service.cancel(run.uid)

    def test_cancel_stops_a_running_step(self, two_steps, admin_user):
        run = workflow_service.start(definition="two", inputs={"topic": "Sofa"}, user=admin_user)  # runner not run
        task = EnrichmentTask.objects.get(pk=run.steps_state[0]["task_id"])
        workflow_service.cancel(run.uid)
        task.refresh_from_db()
        assert task.status == "cancelled"
        workflow_service.on_task_finished(task)  # a late runner hook changes nothing
        assert workflow_queries.get_run(run.uid).status == "cancelled"


class TestStaleAccept:
    """An accept racing cancel / regenerate fails closed: nothing is written, the step does not move."""

    def test_accept_after_cancel_is_refused(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        stale = _proposal(run, "draft")
        workflow_service.cancel(run.uid, user=admin_user)
        with pytest.raises(ValueError, match="cancelled"):
            proposal_service.accept(stale, user=admin_user)
        run = workflow_queries.get_run(run.uid)
        assert (_proposal(run, "draft").status, _states(run)["draft"], run.status) == (
            "rejected",
            "review",
            "cancelled",
        )

    def test_accept_after_regenerate_is_refused(self, two_steps, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user)
        stale = _proposal(run, "draft")
        with run_steps():
            workflow_service.regenerate(run.uid, "draft")
        with pytest.raises(ValueError, match="no longer the step's current output"):
            proposal_service.accept(stale, user=admin_user)
        stale.refresh_from_db()
        run = workflow_queries.get_run(run.uid)
        assert (stale.status, _states(run)["draft"], run.status) == ("superseded", "review", "waiting")


class TestStuckStep:
    def test_a_step_left_running_on_a_closed_task_can_be_regenerated(self, two_steps, toolbox, run_steps, admin_user):
        run = workflow_service.start(definition="two", inputs={"topic": "Sofa"}, user=admin_user)  # runner skipped
        with pytest.raises(ValueError, match="running"):
            workflow_service.regenerate(run.uid, "outline")  # its task is still open
        task_service.transition_status(EnrichmentTask.objects.get(pk=run.steps_state[0]["task_id"]), "cancelled")
        assert _states(workflow_queries.get_run(run.uid))["outline"] == "running"
        with run_steps():
            workflow_service.regenerate(run.uid, "outline")
        run = workflow_queries.get_run(run.uid)
        assert (_states(run), run.status) == ({"outline": "approved", "draft": "review"}, "waiting")


class TestStart:
    def test_inputs_are_validated_against_the_schema(self, two_steps):
        with pytest.raises(ValueError, match="topic"):
            workflow_service.start(definition="two", inputs={})

    def test_unknown_or_inactive_definition(self, two_steps):
        two_steps.active = False
        two_steps.save()
        with pytest.raises(ValueError, match="unknown or inactive"):
            workflow_service.start(definition="two", inputs={"topic": "x"})


class TestArticleFlow:
    """questions → draft → meta (auto) → compose (document) → the target module creates the document."""

    @pytest.fixture
    def article(self, setup):
        steps = [
            wf.step("ask", output="questions"),
            wf.step("draft", needs=("ask",)),
            wf.step("meta", needs=("draft",), gate="auto"),
            wf.step("compose", needs=("draft", "meta"), output="document"),
        ]
        return wf.make_definition("article", steps)

    @pytest.fixture
    def answers_memory(self, monkeypatch):
        recorded = []
        memory = SimpleNamespace(record_answers=lambda **kwargs: recorded.append(kwargs))
        monkeypatch.setitem(sys.modules, "django_enrichment.services.memory_service", memory)
        return recorded

    def _answer(self, run, answers: dict, user) -> ContentProposal:
        questions = _proposal(run, "ask")
        proposal_service.update_value(questions, {**questions.proposed_value, "answers": answers}, user=user)
        return questions

    def test_questions_need_their_required_answers(self, article, toolbox, run_steps, admin_user):
        run = _start(run_steps, admin_user, "article")
        questions = self._answer(run, {"years": "Since 1999"}, admin_user)
        with pytest.raises(AnswersMissingError) as exc:
            proposal_service.accept(questions, user=admin_user)
        assert exc.value.missing == ["tone"]

    def test_end_to_end_creates_the_document(self, article, toolbox, run_steps, admin_user, answers_memory, setup):
        run = _start(run_steps, admin_user, "article")
        questions = self._answer(run, {"years": "Since 1999", "tone": "warm"}, admin_user)
        run = _accept(run_steps, questions, admin_user)
        assert answers_memory[0]["step"] == "ask" and answers_memory[0]["value"]["answers"]["tone"] == "warm"
        draft_task = EnrichmentTask.objects.get(pk=_proposal(run, "draft").task_id)
        assert draft_task.params["inputs"]["ask"]["answers"] == {"years": "Since 1999", "tone": "warm"}
        run = _accept(run_steps, _proposal(run, "draft"), admin_user)
        assert _states(run) == {"ask": "approved", "draft": "approved", "meta": "approved", "compose": "review"}
        document = _proposal(run, "compose")
        assert (document.target_module, document.target_kind, document.cost_usd) == (wf.DOC_MODULE, "document", 0)
        proposal_service.accept(document, user=admin_user)
        document.refresh_from_db()
        run = workflow_queries.get_run(run.uid)
        assert (document.result_ref, run.subject_ref, run.status) == ("doc-1", "doc-1", "done")
        assert setup.documents()["doc-1"]["slug"] == "linen-sofa-care"
        assert run.cost_usd == Decimal("0.03")

    def test_document_accept_after_cancel_writes_nothing(self, article, toolbox, run_steps, admin_user, setup):
        run = _start(run_steps, admin_user, "article")
        run = _accept(run_steps, self._answer(run, {"years": "1999", "tone": "warm"}, admin_user), admin_user)
        run = _accept(run_steps, _proposal(run, "draft"), admin_user)
        document = _proposal(run, "compose")
        workflow_service.cancel(run.uid, user=admin_user)
        with pytest.raises(ValueError, match="cancelled"):
            proposal_service.accept(document, user=admin_user)
        assert setup.documents() == {}
