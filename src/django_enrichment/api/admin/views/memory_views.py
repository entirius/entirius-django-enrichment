# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — memory (contract A4 § 3): list, dismiss / re-activate, delete, promote into the profile.

Thin layer: parse → `memory_service` / `memory_promotion` → Response. ZERO Django models imported.
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pydantic import ValidationError
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.pagination import AdminPageNumberPagination
from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.api.admin.views.profile_views import PROFILE_AREA, profile_body, raise_schema_errors
from django_enrichment.schemas.requests import MemoryPromoteRequest, MemoryUpdateRequest
from django_enrichment.schemas.responses import MemoryEntryResponse, MemoryListResponse, MemoryPromoteResponse
from django_enrichment.services import memory_promotion, memory_service
from django_enrichment.services.profile_service import ProfileValidationError

_TAGS = ["Enrichment Memory"]
_FILTERS = ("channel", "kind", "subject_ref", "status")


def _entry(entry) -> dict:
    return MemoryEntryResponse.model_validate(entry).model_dump(mode="json")


def _parse(schema, data):
    try:
        return schema(**data)
    except ValidationError as exc:
        raise_pydantic_as_drf(exc)


def _get_or_404(pk: int):
    try:
        return memory_service.get_entry(pk)
    except ValueError as exc:
        raise_as_drf(exc)


class MemoryViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = PROFILE_AREA
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="List memory entries",
        parameters=[OpenApiParameter(name, str, OpenApiParameter.QUERY, required=False) for name in _FILTERS],
        responses={200: MemoryListResponse},
    )
    def list(self, request: Request) -> Response:
        filters = {name: request.query_params.get(name) for name in _FILTERS}
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(memory_service.list_entries(**filters), request)
        return paginator.get_paginated_response([_entry(entry) for entry in page])

    @extend_schema(
        tags=_TAGS,
        summary="Dismiss or re-activate an entry",
        request=MemoryUpdateRequest,
        responses={200: MemoryEntryResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, pk: int) -> Response:
        data = _parse(MemoryUpdateRequest, request.data)
        entry = _get_or_404(pk)
        try:
            entry = memory_service.set_status(entry, data.status)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_entry(entry))

    @extend_schema(tags=_TAGS, summary="Delete an entry", responses={204: None, 404: None})
    def destroy(self, request: Request, pk: int) -> Response:
        memory_service.delete_entry(_get_or_404(pk))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=_TAGS,
        summary="Promote a suggestion into the profile (new profile version)",
        request=MemoryPromoteRequest,
        responses={200: MemoryPromoteResponse, 400: None, 404: None},
    )
    def promote(self, request: Request, pk: int) -> Response:
        data = _parse(MemoryPromoteRequest, request.data or {})
        entry = _get_or_404(pk)
        try:
            profile = memory_promotion.promote(entry, target=data.target, user=request.user)
        except ProfileValidationError as exc:
            raise_schema_errors(exc)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response({"entry": _entry(entry), "profile": profile_body(profile)})
