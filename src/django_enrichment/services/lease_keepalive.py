# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Lease keepalive — a subject that makes many paid calls (a step written section by section, the SEO research)
outlives one lease, so the unit's lease is extended between the calls.

The internal consumer opens `kept_alive(heartbeat)` around a unit's subjects; `toolbox_calls` calls `beat()` before
every toolbox call. `beat()` heartbeats at most once per `EVERY_S` and does nothing outside a consumer. A lost lease
raises `LeaseLost` out of the kind — the runner lets it through, so the consumer stops before the next paid call.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

EVERY_S = 60  # well inside ENRICHMENT_LEASE_S (300): one slow call never outlives the lease

_current: ContextVar[Callable[[], None] | None] = ContextVar("enrichment_lease_keepalive", default=None)


@contextmanager
def kept_alive(heartbeat: Callable[[], object]) -> Iterator[None]:
    """Inside the block `beat()` calls `heartbeat`, at most once per `EVERY_S` (counted from the block's start)."""
    last = time.monotonic()

    def throttled() -> None:
        nonlocal last
        if time.monotonic() - last < EVERY_S:
            return
        heartbeat()
        last = time.monotonic()

    token = _current.set(throttled)
    try:
        yield
    finally:
        _current.reset(token)


def beat() -> None:
    """Extend the running unit's lease when it is due; a no-op outside a consumer."""
    if (current := _current.get()) is not None:
        current()
