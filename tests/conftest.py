# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# Shared pytest fixtures for django-enrichment.
# Auth fixtures for the API land in etap-05; here we only need the test adapter + a review user.

import pytest
from django.test import override_settings

from django_enrichment.adapters import clear_cache


@pytest.fixture(autouse=True)
def _celery_eager():
    """Run Celery tasks inline (no broker). The settings flags only bite if the host service wires
    `config_from_object`; standalone module tests don't, so force it on the current app directly."""
    from celery import current_app

    current_app.conf.task_always_eager = True
    current_app.conf.task_eager_propagates = True
    yield


@pytest.fixture
def fake_adapter():
    """Register the in-memory adapter under "pim" and hand it back for seeding.

    Resets the store and the registry import cache around each test so adapter state and the
    per-call settings read never leak between tests.
    """
    from tests import fake_adapter as module

    module.reset()
    clear_cache()
    with override_settings(ENRICHMENT_ADAPTERS={"pim": "tests.fake_adapter"}):
        yield module
    module.reset()
    clear_cache()


@pytest.fixture
def admin_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(username="reviewer", password="pw", is_staff=True)  # noqa: S106


@pytest.fixture
def regular_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(username="customer", password="pw")  # noqa: S106


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def admin_client(api_client, admin_user):
    """Admin-authenticated client (force_authenticate — exercises IsAdminUser, not JWT decode)."""
    api_client.force_authenticate(user=admin_user)
    return api_client


@pytest.fixture
def regular_client(api_client, regular_user):
    """Authenticated non-admin — must be rejected (403) by IsAdminUser."""
    api_client.force_authenticate(user=regular_user)
    return api_client
