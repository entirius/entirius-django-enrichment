# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Shared view helpers: map service-layer `ValueError` to the right DRF exception; serialize a proposal.

Convention across `django_enrichment.services`: `raise ValueError(f"... not found")` means a lookup
miss → 404; anything else is a domain validation failure → 400. The v2 exception handler then emits
the standard `{error, message, debug_id, details}` shape. NEVER `return Response({"detail": ...})`
from a view — that bypasses the handler.
"""

from typing import Any, NoReturn

from rest_framework import exceptions as drf_exceptions

from django_enrichment.schemas.responses import ProposalResponse
from django_enrichment.services import apply_service


def raise_as_drf(exc: ValueError) -> NoReturn:
    msg = str(exc)
    if msg.lower().rstrip(".").endswith("not found"):
        raise drf_exceptions.NotFound(msg) from exc
    raise drf_exceptions.ValidationError(msg) from exc


def serialize_proposal(proposal: Any) -> dict:
    """`ProposalResponse` JSON of a proposal, with the apply choices its adapter offers."""
    body = ProposalResponse.model_validate(proposal)
    body.apply_modes = apply_service.apply_modes(proposal)
    return body.model_dump(mode="json")
