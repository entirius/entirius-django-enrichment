# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `questions` — what the operator must tell us before the content can be written.

One completion turns the brief into questions `{id, question, why, type: text|choice, choices, optional}`;
it sees the profile facts and remembered answers (memory `answer`) so it does not ask again. The artifact
value is `{"questions": [...], "answers": {}, "min_answers": n}`; the operator fills `answers` (`{question id: text}`)
by a manual edit and approves. With `min_answers` (params, default 1 — an article never stands on research alone)
approval needs that many answers to any of the questions, none is required on its own; with 0 (or an artifact made
before `min_answers`) every non-optional question needs its answer → `ANSWERS_MISSING`.
"""

from typing import Any

from pydantic import BaseModel, Field

from django_enrichment.kinds import seams
from django_enrichment.kinds.base import Kind, ProposalDraft, fenced, with_cost
from django_enrichment.kinds.step import StepParams, build_prompt, locator
from django_enrichment.services import toolbox_calls

TARGET_KIND = "artifact"
_QUESTION = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "question": {"type": "string"},
        "why": {"type": "string"},
        "type": {"type": "string", "enum": ["text", "choice"]},
        "choices": {"type": "array", "items": {"type": "string"}},
        "optional": {"type": "boolean"},
    },
    "required": ["id", "question", "why", "type", "choices", "optional"],
    "additionalProperties": False,
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": _QUESTION}},
    "required": ["questions"],
    "additionalProperties": False,
}
_TASK = (
    "Ask the operator the questions needed to write this content well: gaps in the brief, facts, numbers, "
    "examples and opinions only the company knows. Do not ask what the company profile or the remembered "
    "answers already cover. Say in `why` what the answer changes. Use `choice` with `choices` for a pick from "
    "known options; mark nice-to-have questions `optional`."
)


class AnswersMissingError(ValueError):
    """Approval of a questions artifact without its required answers (API: 400 `ANSWERS_MISSING`).

    `minimum` > 0: the required ones are answered, but fewer than the artifact's `min_answers`."""

    def __init__(self, missing: list[str], minimum: int = 0) -> None:
        super().__init__(f"answers missing for: {', '.join(missing)}")
        self.missing = missing
        self.minimum = minimum


class QuestionsOptions(BaseModel):
    rules: list[str] = Field(default_factory=list, description="Rules every run follows", examples=[["Max 8."]])


class QuestionsParams(StepParams):
    min_answers: int = Field(1, ge=0, description="Answers needed to approve, optional questions included")


class QuestionsKind(Kind):
    Options = QuestionsOptions
    Params = QuestionsParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:  # noqa: ARG002
        params = QuestionsParams.model_validate(task.params)
        params.rules = [*QuestionsOptions.model_validate(worker.options).rules, *params.rules]
        prompt = build_prompt(worker, task, params, kind="questions", parts=[_remembered(params), _TASK])
        answer = toolbox_calls.complete(
            system=prompt.system,
            user=prompt.user,
            json_schema=ANSWER_SCHEMA,
            model=worker.model,
            kind="questions",
            task=task,
        )
        questions = normalize(answer.value.get("questions", []))
        value = {"questions": questions, "answers": {}, "min_answers": params.min_answers}
        return with_cost([ProposalDraft(TARGET_KIND, locator(params), value)], answer.cost)


def _remembered(params: StepParams) -> str:
    """Answers the company gave before (memory, instance-wide) — so the model does not ask again."""
    block = seams.recall(channel=params.channel, subject_module="", subject_ref="", kinds=["answer"])
    return fenced("remembered_answers", block) if block else ""


def normalize(questions: list[dict]) -> list[dict]:
    """Unique ids (`q<n>` when blank or repeated); choices only on a `choice` with options."""
    seen: set[str] = set()
    return [_normalized(question, index, seen) for index, question in enumerate(questions, start=1)]


def _normalized(question: dict, index: int, seen: set[str]) -> dict:
    qid = str(question.get("id") or "").strip()
    qid = qid if qid and qid not in seen else f"q{index}"
    seen.add(qid)
    choices = [str(choice) for choice in question.get("choices") or []]
    kind = "choice" if question.get("type") == "choice" and choices else "text"
    return {
        "id": qid,
        "question": str(question.get("question", "")),
        "why": str(question.get("why", "")),
        "type": kind,
        "choices": choices if kind == "choice" else [],
        "optional": bool(question.get("optional", False)),
    }


def validate_answers(value: dict) -> None:
    """At least `min_answers` answers, any of them (capped at the number of questions) — or, without a minimum, every
    non-optional question answered; every choice one of its options. Raises `ValueError`."""
    answers = value.get("answers") or {}
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object {question id: answer}")
    questions = value.get("questions") or []
    if minimum := int(value.get("min_answers") or 0):
        _check_minimum(questions, answers, minimum)
    elif missing := [q.get("id", "") for q in questions if not q.get("optional") and not _answer(answers, q)]:
        raise AnswersMissingError(missing)
    for question in questions:
        answer = _answer(answers, question)
        if question.get("type") == "choice" and answer and answer not in question.get("choices", []):
            raise ValueError(f"the answer to {question.get('id')} is not one of its choices")


def _check_minimum(questions: list[dict], answers: dict, minimum: int) -> None:
    minimum = min(minimum, len(questions))
    unanswered = [q.get("id", "") for q in questions if not _answer(answers, q)]
    if len(questions) - len(unanswered) < minimum:
        raise AnswersMissingError(unanswered, minimum)


def _answer(answers: dict, question: dict) -> str:
    return str(answers.get(question.get("id", "")) or "").strip()
