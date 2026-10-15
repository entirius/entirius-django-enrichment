# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Claim atomicity on postgres: concurrent claims never get the same unit (`skip_locked` needs a real database).

Run with `ENRICHMENT_TEST_DATABASE_URL=postgres://…` (skipped on sqlite, which has no row locks).
"""

import threading

import pytest
from django.db import connection, connections

from django_enrichment.models import WorkUnit
from django_enrichment.services import unit_planner, work_queue_service
from tests.test_runner import _spawn, echo_worker  # noqa: F401 — fixture

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="needs postgres row locks"),
]


def test_concurrent_claims_never_share_a_unit(echo_worker, settings):  # noqa: F811
    settings.ENRICHMENT_UNIT_SIZE = 1
    settings.ENRICHMENT_MAX_INFLIGHT_PER_WORKER = 100
    echo_worker(mode="external")
    unit_planner.plan_task(_spawn([f"SKU-{i}" for i in range(12)]).pk)
    claimed: list[int] = []
    barrier = threading.Barrier(6)

    def grab(name: str) -> None:
        try:
            barrier.wait()
            while unit := work_queue_service.claim("pim.echo", name):
                claimed.append(unit.pk)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == 12 == len(set(claimed))
    assert WorkUnit.objects.filter(status="leased").count() == 12
