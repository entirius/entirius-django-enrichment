# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind `translate` — translate source-language feature texts into target languages.

One toolbox translator call per subject per target language, all fields of the subject together
(`context_mode`), so the model sees the product's fields side by side. Empty source → field skipped.
"""

from typing import Any

from pydantic import BaseModel, Field

from django_enrichment.adapters import get_adapter
from django_enrichment.kinds.base import Kind, ProposalDraft, join_prompts
from django_enrichment.services import toolbox_calls

TARGET_KIND = "attribute_value"


class TranslateOptions(BaseModel):
    fields: list[str] = Field(min_length=1, description="Feature idxs to translate", examples=[["description"]])
    provider: str = Field("", description="Translator provider; blank = channel default", examples=["deepl"])
    formality: str = Field("", description="more / less / default; blank = provider default", examples=["less"])


class TranslateParams(BaseModel):
    channel: str = Field(min_length=1, description="Channel idx", examples=["default"])
    source_language: str = Field(min_length=1, description="Language to read", examples=["pl"])
    target_languages: list[str] = Field(min_length=1, description="Languages to write", examples=[["en", "de"]])
    prompt: str = Field("", description="Extra instructions for this run", examples=["Keep brand names."])


class TranslateKind(Kind):
    Options = TranslateOptions
    Params = TranslateParams

    def run(self, worker: Any, task: Any, subject_ref: str) -> list[ProposalDraft]:
        options = TranslateOptions.model_validate(worker.options)
        params = TranslateParams.model_validate(task.params)
        texts = _source_texts(worker.target_module, subject_ref, options.fields, params)
        if not texts:
            return []
        context = join_prompts(worker.system_prompt, params.prompt)
        drafts: list[ProposalDraft] = []
        for language in params.target_languages:
            translated = toolbox_calls.translate(
                texts, source=params.source_language, target=language, context=context, options=options.model_dump()
            )
            drafts += [_draft(params.channel, language, idx, text) for idx, text in translated.items()]
        return drafts


def _source_texts(target_module: str, subject_ref: str, fields: list[str], params: TranslateParams) -> dict[str, str]:
    adapter = get_adapter(target_module)
    texts = {}
    for feature_idx in fields:
        locator = {"channel": params.channel, "language": params.source_language, "feature_idx": feature_idx}
        current = adapter.read_current(subject_ref=subject_ref, target_kind=TARGET_KIND, target_locator=locator)
        if text := (current.get("text") or "").strip():
            texts[feature_idx] = text
    return texts


def _draft(channel: str, language: str, feature_idx: str, text: str) -> ProposalDraft:
    locator = {"channel": channel, "language": language, "feature_idx": feature_idx}
    return ProposalDraft(target_kind=TARGET_KIND, target_locator=locator, proposed_value={"text": text})
