# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Lease keepalive: a long subject (many paid calls) extends its unit's lease between the calls."""

import json

import httpx
import pytest
from django_utils.toolbox.testing import CANNED_COMPLETION, mock_toolbox

from django_enrichment.kinds import registry
from django_enrichment.models import ContentProposal, WorkUnit
from django_enrichment.services import consumer_service, lease_keepalive, toolbox_calls, work_queue_service
from tests.test_consumer import internal, kicks  # noqa: F401 — fixtures
from tests.test_runner import EchoKind, echo_worker  # noqa: F401 — fixture


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(lease_keepalive.time, "monotonic", clock)
    return clock


class LongKind(EchoKind):
    """A kind whose subject takes long: a minute and more passes before it beats, as between two paid calls."""

    clock: Clock

    def run(self, worker, task, subject_ref):
        self.clock.now += lease_keepalive.EVERY_S + 1
        lease_keepalive.beat()
        return super().run(worker, task, subject_ref)


@pytest.fixture
def long_kind(monkeypatch, clock):
    monkeypatch.setattr(LongKind, "clock", clock, raising=False)
    monkeypatch.setitem(registry._KINDS, "echo", LongKind)


class TestBeat:
    def test_outside_a_consumer_it_does_nothing(self):
        lease_keepalive.beat()

    def test_beats_at_most_once_per_interval(self, clock):
        beats = []
        with lease_keepalive.kept_alive(lambda: beats.append(clock.now)):
            lease_keepalive.beat()
            clock.now += lease_keepalive.EVERY_S + 1
            lease_keepalive.beat()
            lease_keepalive.beat()
        assert beats == [1000.0 + lease_keepalive.EVERY_S + 1]

    def test_the_block_ends_the_keepalive(self, clock):
        beats = []
        with lease_keepalive.kept_alive(lambda: beats.append(1)):
            pass
        clock.now += lease_keepalive.EVERY_S + 1
        lease_keepalive.beat()
        assert beats == []


@pytest.mark.django_db
class TestConsumer:
    def test_a_long_subject_extends_its_lease(self, internal, kicks, long_kind, monkeypatch):  # noqa: F811
        task = internal(["SKU-1"])
        tokens = []
        heartbeat = work_queue_service.heartbeat
        monkeypatch.setattr(work_queue_service, "heartbeat", lambda token: tokens.append(token) or heartbeat(token))
        assert consumer_service.consume("pim.echo", task.lane) == 1
        assert len(tokens) == 2  # before the subject, then once inside it
        assert ContentProposal.objects.count() == 1

    def test_a_lease_lost_inside_a_subject_stops_the_consumer(self, internal, kicks, long_kind, monkeypatch):  # noqa: F811
        task = internal(["SKU-1", "SKU-2"])
        calls = []

        def lose_inside(token):
            calls.append(token)
            if len(calls) > 1:
                raise work_queue_service.LeaseLost("gone")
            return work_queue_service.locked_unit(token)

        monkeypatch.setattr(work_queue_service, "heartbeat", lose_inside)
        assert consumer_service.consume("pim.echo", task.lane) == 1
        task.refresh_from_db()
        assert "failed" not in task.counts  # a lost lease is no failure of the subject
        assert ContentProposal.objects.count() == 0
        assert WorkUnit.objects.filter(status="leased").count() == 1


@pytest.mark.django_db
@pytest.mark.usefixtures("toolbox_settings")
class TestToolboxCalls:
    def test_every_completion_beats_first(self, monkeypatch):
        beats = []
        monkeypatch.setattr(lease_keepalive, "beat", lambda: beats.append(1))
        with mock_toolbox() as router:
            router["complete"].mock(return_value=httpx.Response(200, json={**CANNED_COMPLETION, "parsed": {}}))
            toolbox_calls.complete(system="", user="u", json_schema={"type": "object"}, model="m", kind="generate")
        assert beats == [1]

    def test_every_seo_call_beats_first(self, monkeypatch):
        beats = []
        monkeypatch.setattr(lease_keepalive, "beat", lambda: beats.append(1))
        with mock_toolbox() as router:
            body = json.dumps({"data": {"items": []}, "cost": "0.01", "cached": False})
            router.post("http://toolbox.test/api/ai-seo/v2/admin/shop/search-volume/").mock(
                return_value=httpx.Response(200, content=body)
            )
            toolbox_calls.seo("search-volume/", {"keywords": ["sofa"]})
        assert beats == [1]
