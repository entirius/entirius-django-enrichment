# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Worker API — the pull model shared by external workers (n8n) and our own consumer.

claim → heartbeat → complete | fail. A stale token answers 409 `LEASE_LOST`: stop working on the unit.
Thin layer: parse → service → Response. ZERO Django models imported.
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import extend_schema
from pydantic import ValidationError
from rest_framework import exceptions as drf_exceptions
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.throttling import WorkerQueueThrottle
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.kinds import ProposalDraft
from django_enrichment.schemas.requests import ClaimRequest, CompleteRequest, FailRequest, LeaseRequest
from django_enrichment.schemas.responses import (
    ClaimedUnitResponse,
    FailedUnitResponse,
    HeartbeatResponse,
    UnitOutcomeResponse,
)
from django_enrichment.services import unit_payload, unit_result_service, work_queue_service
from django_enrichment.services.queue_types import LeaseLost, SubjectResult

_TAGS = ["Enrichment Work Queue"]


class LeaseLostError(drf_exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "LEASE_LOST"
    default_detail = "The lease is gone: the unit was given to someone else or expired. Stop working on it."


def _parse(schema, request: Request):
    try:
        return schema(**request.data)
    except ValidationError as exc:
        raise_pydantic_as_drf(exc)


def _results(data: CompleteRequest) -> list[SubjectResult]:
    return [
        SubjectResult(
            r.subject_ref,
            drafts=[
                ProposalDraft(d.target_kind, d.target_locator, d.proposed_value, d.subject_label) for d in r.drafts
            ],
            error_code=r.error_code,
            error_id=r.error_id,
        )
        for r in data.results
    ]


class WorkQueueViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    throttle_classes = [WorkerQueueThrottle]
    access_area = "enrichment.proposals"
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="Claim one unit of work",
        request=ClaimRequest,
        responses={200: ClaimedUnitResponse, 204: None, 404: None},
    )
    def claim(self, request: Request) -> Response:
        data = _parse(ClaimRequest, request)
        try:
            unit = work_queue_service.claim(data.worker, data.instance, data.version, data.lane)
        except ValueError as exc:
            raise_as_drf(exc)
        if unit is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(ClaimedUnitResponse(**unit_payload.describe(unit)).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Extend a lease",
        request=LeaseRequest,
        responses={200: HeartbeatResponse, 409: None},
    )
    def heartbeat(self, request: Request) -> Response:
        data = _parse(LeaseRequest, request)
        try:
            unit = work_queue_service.heartbeat(data.token)
        except LeaseLost as exc:
            raise LeaseLostError from exc
        return Response(HeartbeatResponse(lease_until=unit.lease_until).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Report the results of a unit",
        request=CompleteRequest,
        responses={200: UnitOutcomeResponse, 400: None, 409: None},
    )
    def complete(self, request: Request) -> Response:
        data = _parse(CompleteRequest, request)
        try:
            outcome = unit_result_service.complete(data.token, _results(data))
        except LeaseLost as exc:
            raise LeaseLostError from exc
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(UnitOutcomeResponse(**outcome).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Report that a unit could not be done",
        request=FailRequest,
        responses={200: FailedUnitResponse, 409: None},
    )
    def fail(self, request: Request) -> Response:
        data = _parse(FailRequest, request)
        try:
            unit = work_queue_service.fail(data.token, data.code, data.message, data.retryable)
        except LeaseLost as exc:
            raise LeaseLostError from exc
        body = FailedUnitResponse(
            unit_id=unit.pk, status=unit.status, attempts=unit.attempts, not_before=unit.not_before
        )
        return Response(body.model_dump(mode="json"))
