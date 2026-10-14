# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind contract: the code a `Worker` runs, one subject at a time.

A kind validates `Worker.options` with `Options` and `task.params` with `Params`, does the work for
one subject and returns `ProposalDraft`s. It never writes proposals — the runner hands every draft
to `proposal_service.intake` (or `intake_media` when the draft carries a `media_file`), so snapshot,
supersede, staging and progress counts stay in one place.
"""

from dataclasses import dataclass, field
from typing import Any

from django.core.files import File
from pydantic import BaseModel


@dataclass
class ProposalDraft:
    target_kind: str
    target_locator: dict
    proposed_value: dict
    subject_label: str = field(default="")
    media_file: File | None = field(default=None)  # binary of a media draft (video) → staged at intake


class Kind:
    """Base kind. Subclasses set `Options` / `Params` and implement `run`."""

    Options: type[BaseModel]
    Params: type[BaseModel]

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        raise NotImplementedError


def join_prompts(*parts: str) -> str:
    """Prompt layers (worker calibration, then this run's prompt), blanks dropped."""
    return "\n\n".join(part for part in parts if part)
