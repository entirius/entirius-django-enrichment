# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Built-in adapter `"enrichment"` — workflow artifacts (contract A4 § 6).

An artifact is an intermediate step output (a brief's keywords, an outline, questions with answers) that
lives only in its proposal: `target_kind="artifact"`, `subject_ref="run:<uid>"`, locator `{"step"}`, value
`{"text"}` | `{"data"}` | `{"questions", "answers"}`. There is nothing to write, so `apply` only checks
that a questions artifact is answered; approving it advances the run (`workflow_service`, called by
`apply_service` for every `run:` proposal). An approved step cannot be undone → `revert` refuses.
"""

from typing import Any

from django_enrichment.kinds.questions import validate_answers

KEY = "enrichment"
TARGET_KIND = "artifact"


def resolve_targets(scope_spec: dict, page: int = 1) -> list[Any]:
    """`list` mode only: the refs themselves, on one page."""
    return list(scope_spec.get("refs", [])) if page == 1 else []


def find_gaps(check: str, params: dict, scope: dict) -> list[Any]:  # noqa: ARG001 — artifacts have no gaps
    return []


def read_current(*, subject_ref: str, target_kind: str, target_locator: dict) -> dict:  # noqa: ARG001
    """An artifact has no live value outside its proposal — always `{}` (so it never drifts)."""
    return {}


def apply(proposal: Any) -> None:
    """Approve: a questions artifact needs its required answers (`AnswersMissingError` otherwise)."""
    if "questions" in proposal.proposed_value:
        validate_answers(proposal.proposed_value)


def revert(proposal: Any) -> None:  # noqa: ARG001 — refusing is the contract
    raise ValueError("an approved workflow step cannot be undone; regenerate the step instead")
