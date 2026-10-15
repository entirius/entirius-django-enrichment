# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""This month's AI usage of the instance's toolbox client — the CMS header counter.

Tokens come from the tracker `usage/` rows whose units are LLM tokens; the cost is the billed amount (provider
cost plus the toolbox markup) from `billing/client/`, `None` while the toolbox has billing off for the client.
Cached briefly: every CMS tab polls it.
"""

from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.utils import timezone
from django_utils.toolbox import ToolboxServerError

from django_enrichment.services import toolbox_calls

CACHE_KEY = "enrichment:ai-usage:{year_month}"
CACHE_TTL_S = 60
# Rows counted as tokens: completion, and translator unless DeepL (characters); seo counts requests, media seconds.
_TOKEN_TOOLS = frozenset({"completion", "translator"})
_NON_TOKEN_PROVIDERS = frozenset({"deepl"})
_DEFAULT_CURRENCY = "USD"


def current_month() -> dict:
    """`{period, tokens, cost_usd, cost_since, currency}` for the current UTC month (the tracker's month)."""
    year_month = timezone.now().strftime("%Y-%m")
    key = CACHE_KEY.format(year_month=year_month)
    if (cached := cache.get(key)) is not None:
        return cached
    month = _month(year_month)
    cache.set(key, month, CACHE_TTL_S)
    return month


def _month(year_month: str) -> dict:
    usage, billing = toolbox_calls.tracker_month(year_month)
    try:
        return {"period": year_month, "tokens": _tokens(usage), **_billed(billing)}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ToolboxServerError(200, "Toolbox usage response does not match the contract") from None


def _tokens(usage: dict) -> int:
    return sum(int(row["total_input"]) + int(row["total_output"]) for row in usage["breakdown"] if _is_tokens(row))


def _is_tokens(row: dict) -> bool:
    return row["tool_name"] in _TOKEN_TOOLS and row["provider"] not in _NON_TOKEN_PROVIDERS


def _billed(billing: dict | None) -> dict:
    """`cost_since` is set when billing started inside the month — the cost covers only part of it."""
    if billing is None:
        return {"cost_usd": None, "cost_since": None, "currency": _DEFAULT_CURRENCY}
    date_from = billing["period"]["date_from"]
    return {
        "cost_usd": Decimal(billing["totals"]["amount_due_usd"]),
        "cost_since": None if date_from.endswith("-01") else date_from,
        "currency": billing.get("currency") or _DEFAULT_CURRENCY,
    }
