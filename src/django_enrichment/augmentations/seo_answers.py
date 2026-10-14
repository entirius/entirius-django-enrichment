# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Boundary models of the `seo_brief` augmentation: the two completion answers and the toolbox SEO `data`.

A response that does not fit raises `pydantic.ValidationError` → the subject fails with `SEO_BRIEF_BAD_RESPONSE`.
"""

from pydantic import BaseModel


class SeedsAnswer(BaseModel):
    seeds: list[str]


class EvaluateAnswer(BaseModel):
    primary: str
    secondary: list[str]
    intent: str
    serp_notes: str
    gaps: list[str]


class KeywordItem(BaseModel):
    keyword: str
    search_volume: int | None = None
    competition: float | str | None = None


class SeoItems(BaseModel):
    """`data` of `search-volume/` and `keyword-ideas/`."""

    items: list[KeywordItem]


class OrganicResult(BaseModel):
    rank: int | None = None
    domain: str | None = None
    title: str | None = None
    snippet: str | None = None


class SerpData(BaseModel):
    """`data` of `serp/`."""

    organic: list[OrganicResult] = []
    people_also_ask: list[str] = []
    ai_overview: bool | None = None
