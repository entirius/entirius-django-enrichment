# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — ContentProposal (intake / list / retrieve / accept / reject / bulk).

Thin layer: parse → service → Response. ZERO Django models imported. Intake is the worker-traffic
path (throttled + idempotent on `external_ref`); accept routes through the drift-aware apply.
"""

from decimal import Decimal, InvalidOperation

from django.http import FileResponse
from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pydantic import ValidationError
from rest_framework import exceptions as drf_exceptions
from rest_framework import status, viewsets
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.pagination import AdminPageNumberPagination
from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.throttling import BulkActionThrottle, IntakeThrottle
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.requests import (
    BulkAcceptRequest,
    BulkRejectRequest,
    BulkUndoRequest,
    ProposalIntakeRequest,
    ProposalMediaIntakeRequest,
    RejectRequest,
)
from django_enrichment.schemas.responses import (
    BulkAcceptResponse,
    BulkRejectResponse,
    BulkUndoResponse,
    ProposalListResponse,
    ProposalResponse,
)
from django_enrichment.services import proposal_service, staging_store, task_service

_TAGS = ["Enrichment Proposals"]

# Sort fields a client may pass — validated before reaching `order_by` so a bad field can't leak
# column names through an ORM error (django-rest-api §: sort allowlist).
_ALLOWED_ORDERING = frozenset(
    {"created_at", "-created_at", "confidence", "-confidence", "status", "-status", "subject_ref", "-subject_ref"}
)


def _serialize(proposal) -> dict:
    return ProposalResponse.model_validate(proposal).model_dump(mode="json")


def _get_proposal_or_404(proposal_id: int):
    try:
        return proposal_service.get_proposal(proposal_id)
    except ValueError as exc:
        raise_as_drf(exc)


def _parse_confidence_min(raw: str | None) -> Decimal | None:
    """Validate the query-param at the boundary — a bad value is a 400, not a DB cast 500."""
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise drf_exceptions.ValidationError("confidence_min must be a decimal") from exc


class ProposalViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    # Multipart is needed for `upload_media`; JSON/form keep every other action working as before.
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    serializer_class = None

    def get_throttles(self):
        # Worker-traffic paths (text intake + media upload) are throttled; so are the mass-mutating
        # bulk endpoints (etap-09 — each call can fan a canvas over the source module). Operator
        # single-row review (accept/reject/list) is not throttled.
        if self.action in ("create", "upload_media"):
            return [IntakeThrottle()]
        if self.action in ("bulk_accept", "bulk_undo", "bulk_reject"):
            return [BulkActionThrottle()]
        return []

    @extend_schema(
        tags=_TAGS,
        summary="Intake a proposal (worker)",
        request=ProposalIntakeRequest,
        responses={201: ProposalResponse, 400: None},
    )
    def create(self, request: Request) -> Response:
        try:
            data = ProposalIntakeRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            task = task_service.get_task(data.task_id) if data.task_id is not None else None
        except ValueError as exc:
            raise_as_drf(exc)
        try:
            proposal = proposal_service.intake(
                target_module=data.target_module,
                subject_ref=data.subject_ref,
                target_kind=data.target_kind,
                target_locator=data.target_locator,
                proposed_value=data.proposed_value,
                task=task,
                target_type=data.target_type,
                subject_label=data.subject_label,
                subject_url=data.subject_url,
                source=data.source,
                confidence=data.confidence,
                batch_id=data.batch_id,
                external_ref=data.external_ref,
            )
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(proposal), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Intake a media proposal (worker, multipart)",
        request=ProposalMediaIntakeRequest,
        responses={201: ProposalResponse, 400: None},
    )
    def upload_media(self, request: Request) -> Response:
        """Multipart media intake — the binary (`file` part) is staged; PIM is untouched until accept."""
        upload = request.FILES.get("file")
        if upload is None:
            raise drf_exceptions.ValidationError("file is required")
        try:
            data = ProposalMediaIntakeRequest(**{k: v for k, v in request.data.items() if k != "file"})
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            task = task_service.get_task(data.task_id) if data.task_id is not None else None
        except ValueError as exc:
            raise_as_drf(exc)
        try:
            proposal = proposal_service.intake_media(
                file=upload,
                target_module=data.target_module,
                subject_ref=data.subject_ref,
                target_locator=data.target_locator,
                proposed_value=data.proposed_value,
                task=task,
                target_type=data.target_type,
                subject_label=data.subject_label,
                subject_url=data.subject_url,
                source=data.source,
                confidence=data.confidence,
                batch_id=data.batch_id,
                external_ref=data.external_ref,
            )
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(proposal), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Stream a proposal's staged binary (CMS before/after preview)",
        responses={200: None, 404: None},
    )
    def staged_file(self, request: Request, pk: int) -> FileResponse:
        """Serve the staged binary for review (admin-only). The CMS fetches this as the 'after' image."""
        proposal = _get_proposal_or_404(pk)
        if not proposal.staged_file or not staging_store.exists(proposal.staged_file):
            raise drf_exceptions.NotFound("no staged file for this proposal")
        # open_file owns the handle (and re-validates the ref); FileResponse closes it when streamed.
        return FileResponse(staging_store.open_file(proposal.staged_file))

    @extend_schema(
        tags=_TAGS,
        summary="List proposals for review",
        parameters=[
            OpenApiParameter("status", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("target_module", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("target_kind", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("batch_id", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("source", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("confidence_min", float, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("search", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("ordering", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: ProposalListResponse},
    )
    def list(self, request: Request) -> Response:
        ordering = request.query_params.get("ordering", "-created_at")
        if ordering not in _ALLOWED_ORDERING:
            raise drf_exceptions.ValidationError(f"ordering must be one of {sorted(_ALLOWED_ORDERING)}")
        qp = request.query_params
        qs = proposal_service.list_for_review(
            status=qp.get("status", "pending"),
            module=qp.get("target_module"),
            target_kind=qp.get("target_kind"),
            batch_id=qp.get("batch_id"),
            source=qp.get("source"),
            confidence_min=_parse_confidence_min(qp.get("confidence_min")),
            search=qp.get("search"),
            ordering=ordering,
        )
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response([_serialize(p) for p in page])

    @extend_schema(tags=_TAGS, summary="Retrieve proposal", responses={200: ProposalResponse, 404: None})
    def retrieve(self, request: Request, pk: int) -> Response:
        return Response(_serialize(_get_proposal_or_404(pk)))

    @extend_schema(tags=_TAGS, summary="Accept proposal (drift-aware apply)", responses={200: ProposalResponse})
    def accept(self, request: Request, pk: int) -> Response:
        proposal = _get_proposal_or_404(pk)
        try:
            proposal = proposal_service.accept(proposal, user=request.user)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(proposal))

    @extend_schema(tags=_TAGS, summary="Reject proposal", request=RejectRequest, responses={200: ProposalResponse})
    def reject(self, request: Request, pk: int) -> Response:
        try:
            data = RejectRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        proposal = _get_proposal_or_404(pk)
        try:
            proposal = proposal_service.reject(proposal, data.reason, user=request.user)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(proposal))

    @extend_schema(
        tags=_TAGS,
        summary="Bulk accept (sync below threshold, else async canvas)",
        request=BulkAcceptRequest,
        responses={200: BulkAcceptResponse},
    )
    def bulk_accept(self, request: Request) -> Response:
        try:
            data = BulkAcceptRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        # `mode` in the body tells the client whether counts are inline (sync) or to poll (async) — a
        # single 200 keeps the client simple (no 200-vs-202 branching). Round-trip through the schema
        # so the response contract (every field present) is enforced here, not only by the service.
        result = proposal_service.bulk_accept(data.to_filters(), user=request.user)
        return Response(BulkAcceptResponse.model_validate(result).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Bulk undo (sync below threshold, else async canvas)",
        request=BulkUndoRequest,
        responses={200: BulkUndoResponse},
    )
    def bulk_undo(self, request: Request) -> Response:
        """Revert the filtered APPLIED set — single-level undo, drift-blocked per item (etap-09)."""
        try:
            data = BulkUndoRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        result = proposal_service.bulk_undo(data.to_filters(), user=request.user)
        return Response(BulkUndoResponse.model_validate(result).model_dump(mode="json"))

    @extend_schema(tags=_TAGS, summary="Bulk reject", request=BulkRejectRequest, responses={200: BulkRejectResponse})
    def bulk_reject(self, request: Request) -> Response:
        try:
            data = BulkRejectRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        result = proposal_service.bulk_reject(data.to_filters(), data.reason, user=request.user)
        return Response(result)
