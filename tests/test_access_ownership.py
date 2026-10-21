# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The module owns its access rules: areas on its AppConfig, an `access_area` on every admin view."""

import pytest

pytest.importorskip("django_access")

from django.apps import apps  # noqa: E402
from django.urls import resolve  # noqa: E402
from django_access.catalogue.areas import DEFAULT_AREAS, Area  # noqa: E402
from django_access.catalogue.scopes import DEFAULT_SCOPES, TokenScope  # noqa: E402
from django_access.services import route_map  # noqa: E402
from django_access.testing import assert_routes_covered  # noqa: E402

from django_enrichment.api.admin.views.memory_views import MemoryViewSet  # noqa: E402
from django_enrichment.api.admin.views.profile_views import PROFILE_AREA, ProfileViewSet  # noqa: E402

LABEL = "django_enrichment"


def test_admin_routes_are_covered_by_own_declarations():
    assert_routes_covered(LABEL, urlconf="django_enrichment.urls", require_own=True)


def test_declarations_match_the_access_defaults():
    """The access defaults plus `enrichment.profile`; delete with those defaults in a later access release."""
    config = apps.get_app_config(LABEL)
    areas = [Area(**{**item, "module": LABEL}) for item in config.access_areas]
    scopes = [TokenScope(**{**item, "module": LABEL}) for item in getattr(config, "access_token_scopes", [])]
    defaults = [item for item in DEFAULT_AREAS if item.module == LABEL]
    assert [item for item in areas if item.key != PROFILE_AREA] == defaults
    assert scopes == [item for item in DEFAULT_SCOPES if item.module == LABEL]


def test_profile_and_memory_need_the_profile_area():
    assert ProfileViewSet.access_area == MemoryViewSet.access_area == PROFILE_AREA
    assert PROFILE_AREA in {item["key"] for item in apps.get_app_config(LABEL).access_areas}


def _needed(path: str, method: str) -> str | None:
    match = resolve(path, urlconf="django_enrichment.urls")
    return route_map.required_permission(route_map.classify(match.route, match.func), method)


def test_rule_promotion_needs_the_profile_area_not_proposals():
    """Promoting to a worker or template changes instance-wide instructions: proposals:write alone is refused."""
    assert _needed("/api/enrichment/v2/admin/proposals/1/rules/promote/", "POST") == f"{PROFILE_AREA}:write"
    assert _needed("/api/enrichment/v2/admin/proposals/1/rules/", "PUT") == "enrichment.proposals:write"


def test_retry_needs_the_proposals_area_like_spawn():
    """A retry spawns paid work: write access to tasks, the same area as `POST tasks/`."""
    assert _needed("/api/enrichment/v2/admin/tasks/1/retry/", "POST") == "enrichment.proposals:write"
    assert _needed("/api/enrichment/v2/admin/tasks/", "POST") == "enrichment.proposals:write"
