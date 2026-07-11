# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Shared view helper: map service-layer `ValueError` to the right DRF exception.

Convention across `django_enrichment.services`: `raise ValueError(f"... not found")` means a lookup
miss → 404; anything else is a domain validation failure → 400. The v2 exception handler then emits
the standard `{error, message, debug_id, details}` shape. NEVER `return Response({"detail": ...})`
from a view — that bypasses the handler.
"""

from typing import NoReturn

from rest_framework import exceptions as drf_exceptions


def raise_as_drf(exc: ValueError) -> NoReturn:
    msg = str(exc)
    if msg.lower().rstrip(".").endswith("not found"):
        raise drf_exceptions.NotFound(msg) from exc
    raise drf_exceptions.ValidationError(msg) from exc
