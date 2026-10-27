# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""`GET usage/month/` — this month's AI usage from the toolbox tracker (respx), cached, quiet when down."""

import httpx
import pytest
import respx
from django.core.cache import cache
from django.utils import timezone
from django_utils.toolbox.testing import error_response

URL = "/api/enrichment/v2/admin/usage/month/"
TRACKER = "http://toolbox.test/api/ai-tracker/v2/admin/shop"
USAGE = {
    "channel_idx": "shop",
    "year_month": "2026-10",
    "total_cost_usd": 3.6,
    "breakdown": [
        {"provider": "fake", "tool_name": "completion", "total_cost": 3.5, "total_input": 1000, "total_output": 200},
        {"provider": "gemini", "tool_name": "translator", "total_cost": 0.05, "total_input": 30, "total_output": 20},
        {"provider": "deepl", "tool_name": "translator", "total_cost": 0.04, "total_input": 9000, "total_output": 0},
        {"provider": "fake", "tool_name": "seo", "total_cost": 0.01, "total_input": 0, "total_output": 57},
    ],
}


def _billing(date_from: str) -> dict:
    return {
        "client_idx": "shop",
        "currency": "USD",
        "period": {"date_from": date_from, "date_to": "2026-10-31", "tz": "UTC", "days": 17},
        "totals": {"billed_usd": "4.31840000", "amount_due_usd": "4.32", "is_partial": False},
    }


@pytest.fixture(autouse=True)
def _empty_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tracker(toolbox_settings):
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{TRACKER}/usage/", name="usage").mock(return_value=httpx.Response(200, json=USAGE))
        router.get(f"{TRACKER}/billing/client/", name="billing").mock(
            return_value=httpx.Response(200, json=_billing("2026-10-01"))
        )
        yield router


def test_tokens_and_billed_cost_of_the_current_month(admin_client, tracker):
    response = admin_client.get(URL)

    assert response.status_code == 200
    assert response.json() == {
        "period": timezone.now().strftime("%Y-%m"),
        "tokens": 1250,
        "cost_usd": "4.32",
        "cost_since": None,
        "currency": "USD",
    }
    year, month = timezone.now().strftime("%Y-%m").split("-")
    assert dict(tracker["usage"].calls.last.request.url.params) == {"year": year, "month": month}
    assert tracker["billing"].calls.last.request.url.params["granularity"] == "none"


def test_billing_started_this_month_sets_cost_since(admin_client, tracker):
    tracker["billing"].mock(return_value=httpx.Response(200, json=_billing("2026-10-15")))

    assert admin_client.get(URL).json()["cost_since"] == "2026-10-15"


def test_billing_off_gives_tokens_without_cost(admin_client, tracker):
    tracker["billing"].mock(return_value=error_response(404, "NOT_FOUND", "Billing is not enabled for this client."))

    body = admin_client.get(URL).json()

    assert (body["tokens"], body["cost_usd"], body["cost_since"]) == (1250, None, None)


def test_second_read_is_served_from_cache(admin_client, tracker):
    admin_client.get(URL)
    admin_client.get(URL)

    assert (tracker["usage"].call_count, tracker["billing"].call_count) == (1, 1)


def test_toolbox_down_is_a_quiet_503(admin_client, tracker):
    tracker["usage"].mock(side_effect=httpx.ConnectError("refused"))

    response = admin_client.get(URL)

    assert response.status_code == 503
    assert response.json()["error"] == "AI_USAGE_UNAVAILABLE"
    assert "refused" not in response.content.decode()
    assert tracker["usage"].call_count == 1  # read-only counter: no retry loop on a dead toolbox


def test_contract_mismatch_is_a_503_not_a_500(admin_client, tracker):
    tracker["usage"].mock(return_value=httpx.Response(200, json={"unexpected": True}))

    response = admin_client.get(URL)

    assert (response.status_code, response.json()["error"]) == (503, "AI_USAGE_UNAVAILABLE")


def test_failures_are_not_cached(admin_client, tracker):
    tracker["usage"].mock(side_effect=httpx.ConnectError("refused"))
    admin_client.get(URL)
    tracker["usage"].mock(return_value=httpx.Response(200, json=USAGE))

    assert admin_client.get(URL).status_code == 200


def test_disabled_toolbox_keeps_its_code(admin_client, toolbox_settings):
    toolbox_settings.AI_TOOLBOX_ENABLED = False

    response = admin_client.get(URL)

    assert (response.status_code, response.json()["error"]) == (503, "AI_TOOLBOX_DISABLED")


def test_non_admin_is_rejected(regular_client):
    assert regular_client.get(URL).status_code == 403
