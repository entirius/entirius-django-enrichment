# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind contract: the code a `Worker` runs, one subject at a time.

A kind validates `Worker.options` with `Options` and `task.params` with `Params`, does the work for
one subject and returns `ProposalDraft`s. It never writes proposals — the runner hands every draft
to `proposal_service.intake` (or `intake_media` when the draft carries a `media_file`), so snapshot,
supersede, staging and progress counts stay in one place.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.core.files import File
from pydantic import BaseModel

_COST_QUANTUM = Decimal("0.00000001")  # decimal_places of the cost columns


@dataclass
class ProposalDraft:
    target_kind: str
    target_locator: dict
    proposed_value: dict
    subject_label: str = field(default="")
    media_file: File | None = field(default=None)  # binary of a media draft (video) → staged at intake
    cost_usd: Decimal | None = field(default=None)  # this draft's share of its toolbox call's cost (None = unknown)


class SubjectError(Exception):
    """A subject the kind cannot work on; `code` lands in `task.errors` (e.g. `FIELD_TYPE`)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class Kind:
    """Base kind. Subclasses set `Options` / `Params` and implement `run`."""

    Options: type[BaseModel]
    Params: type[BaseModel]

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        raise NotImplementedError


def with_cost(drafts: list[ProposalDraft], cost: Decimal | None) -> list[ProposalDraft]:
    """Split one toolbox call's cost evenly over the drafts it produced (contract A4 § 1)."""
    share = None if cost is None or not drafts else (cost / len(drafts)).quantize(_COST_QUANTUM)
    for draft in drafts:
        draft.cost_usd = share
    return drafts


def join_prompts(*parts: str) -> str:
    """Prompt layers (worker calibration, then this run's prompt), blanks dropped."""
    return "\n\n".join(part for part in parts if part)


def fenced(tag: str, text: str) -> str:
    """`text` inside `<tag>` with its own closing tag neutralised, so data cannot break out of the block."""
    safe = re.sub(rf"<\s*/\s*{tag}", rf"<\\/{tag}", text, flags=re.IGNORECASE)
    return f"<{tag}>\n{safe}\n</{tag}>"
