# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `compose` — no AI: the article's markdown + its meta data → one document for the target module.

Value (contract A4 § 6): `{language, title, slug, body_markdown, meta, faq}` with `target_kind="document"`;
the target module's adapter creates the document on approval and returns its ref (`result_ref`). The body
comes from the `{"text"}` input of `body_step`, the meta from the `{"data"}` input of `meta_step`
(`{title, description, og_title, og_description, slug, faq}`, all optional).
"""

import re
from decimal import Decimal
from typing import Any

from django.utils.text import slugify
from pydantic import BaseModel, Field

from django_enrichment.kinds.base import Kind, ProposalDraft, SubjectError
from django_enrichment.kinds.step import StepParams

TARGET_KIND = "document"
_OWN_KEYS = frozenset({"title", "slug", "faq"})
_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)


class ComposeOptions(BaseModel):
    pass


class ComposeParams(StepParams):
    body_step: str = Field("draft", description="Step whose markdown is the body", examples=["draft"])
    meta_step: str = Field("meta", description="Step whose data is the meta", examples=["meta"])
    language: str = Field("", description="Document language; blank = brief.language", examples=["en"])


class ComposeKind(Kind):
    Options = ComposeOptions
    Params = ComposeParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:  # noqa: ARG002
        params = ComposeParams.model_validate(task.params)
        body = str((params.inputs.get(params.body_step) or {}).get("text") or "")
        if not body.strip():
            raise SubjectError("INPUT_MISSING", f"no markdown from step {params.body_step!r}")
        meta = (params.inputs.get(params.meta_step) or {}).get("data") or {}
        value = document(params, body, meta)
        draft = ProposalDraft(TARGET_KIND, {"channel": params.channel}, value, subject_label=value["title"])
        draft.cost_usd = Decimal(0)  # no toolbox call
        return [draft]


def document(params: ComposeParams, body: str, meta: dict) -> dict:
    title = str(meta.get("title") or _first_heading(body) or params.brief.get("title") or "")
    return {
        "language": params.language or str(params.brief.get("language") or ""),
        "title": title,
        "slug": slugify(str(meta.get("slug") or title)),
        "body_markdown": body,
        "meta": {key: value for key, value in meta.items() if key not in _OWN_KEYS},
        "faq": list(meta.get("faq") or []),
    }


def _first_heading(markdown: str) -> str:
    match = _HEADING.search(markdown)
    return match.group(1).strip() if match else ""
