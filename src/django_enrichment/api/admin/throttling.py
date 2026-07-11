# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Throttle for proposal intake.

`POST /proposals/` is the worker-traffic path (n8n submits proposals continuously). A leaked
admin token or a runaway worker could otherwise hammer it and burn DB/upstream quota, so intake
is rate-limited. The class-level `rate` is a safety net: if the service forgets to configure the
scope, throttling still applies rather than silently turning off.

Service override: `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["enrichment_intake"] = "..."`.
"""

from rest_framework.throttling import UserRateThrottle


class IntakeThrottle(UserRateThrottle):
    scope = "enrichment_intake"
    rate = "100/min"

    def get_rate(self) -> str:
        try:
            return super().get_rate()
        except Exception:  # noqa: BLE001 — DRF raises ImproperlyConfigured when the scope is unset
            return self.rate


class SpawnThrottle(UserRateThrottle):
    """Caps task spawning — `POST /tasks/` and `POST /tasks/import-csv/` (etap-11).

    The n8n analyzer loop hits `POST /tasks` repeatedly with a `batch_key` (most calls coalesce into
    an open rolling task rather than create). The auth gate (admin) holds, but the bus runs on a
    service account, so a leaked token or a runaway loop could otherwise spam the endpoint. A modest
    cap bounds that without impeding a human operator. Same class-level safety-net pattern as below.

    Service override: `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["enrichment_spawn"] = "..."`.
    """

    scope = "enrichment_spawn"
    rate = "120/min"

    def get_rate(self) -> str:
        try:
            return super().get_rate()
        except Exception:  # noqa: BLE001 — DRF raises ImproperlyConfigured when the scope is unset
            return self.rate


class BulkActionThrottle(UserRateThrottle):
    """Caps the mass-mutating bulk endpoints (accept/undo/reject — etap-09).

    Each call mutates the whole filtered set (and can fan out a Celery canvas hitting the source
    module per row). The auth gate (admin) holds, but the bus runs on a service account, so a leaked
    token could loop bulk-undo to exhaust the queue / hammer PIM. A modest cap stops a runaway token
    without impeding a human operator. Same class-level safety-net pattern as `IntakeThrottle`.
    """

    scope = "enrichment_bulk"
    rate = "30/min"

    def get_rate(self) -> str:
        try:
            return super().get_rate()
        except Exception:  # noqa: BLE001 — DRF raises ImproperlyConfigured when the scope is unset
            return self.rate
