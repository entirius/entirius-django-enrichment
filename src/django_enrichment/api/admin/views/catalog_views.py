# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — field catalog passthrough (contract A3 § 2): `GET fields/`, `GET languages/`, `POST subjects/resolve/`.

Thin layer over `catalog_service`. An adapter without the extra → 404 `NOT_SUPPORTED` (v2 error shape).
"""

import uuid

from django_utils.api.v2_errors import ErrorResponse, raise_pydantic_as_drf
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pydantic import ValidationError
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.throttling import LookupThrottle
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.requests import CatalogQuery, SubjectResolveRequest
from django_enrichment.schemas.responses import FieldResponse, SubjectResolveResponse
from django_enrichment.services import catalog_service
from django_enrichment.services.catalog_service import NotSupportedError

_TAGS = ["Enrichment Field Catalog"]
_MODULE = OpenApiParameter("target_module", str, OpenApiParameter.QUERY, required=True)
_CHANNEL = OpenApiParameter("channel", str, OpenApiParameter.QUERY, required=True)


def _query(request: Request) -> CatalogQuery:
    try:
        return CatalogQuery(**request.query_params.dict())
    except ValidationError as exc:
        raise_pydantic_as_drf(exc)


def _not_supported(message: str = "This module does not describe its fields.") -> Response:
    body = ErrorResponse(
        error="NOT_SUPPORTED",
        message=message,
        debug_id=uuid.uuid4().hex[:8],
        details=[],
    )
    return Response(body.model_dump(), status=status.HTTP_404_NOT_FOUND)


class CatalogViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    serializer_class = None

    def get_throttles(self):
        return [LookupThrottle()] if self.action == "resolve_subjects" else []

    @extend_schema(
        tags=_TAGS,
        summary="Writable fields of a module (field catalog)",
        parameters=[_MODULE, _CHANNEL, OpenApiParameter("language", str, OpenApiParameter.QUERY, required=False)],
        responses={200: FieldResponse, 400: None, 404: None},
    )
    def fields(self, request: Request) -> Response:
        query = _query(request)
        try:
            fields = catalog_service.describe_fields(**query.model_dump())
        except NotSupportedError:
            return _not_supported()
        except ValueError as exc:
            raise_as_drf(exc)
        return Response([FieldResponse(**field).model_dump(mode="json", exclude_none=True) for field in fields])

    @extend_schema(
        tags=_TAGS,
        summary="Languages of a channel (main first)",
        parameters=[_MODULE, _CHANNEL],
        responses={200: {"type": "array", "items": {"type": "string"}}, 400: None, 404: None},
    )
    def languages(self, request: Request) -> Response:
        query = _query(request)
        try:
            languages = catalog_service.channel_languages(target_module=query.target_module, channel=query.channel)
        except NotSupportedError:
            return _not_supported()
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(languages)

    @extend_schema(
        tags=_TAGS,
        summary="Which refs (SKUs) exist in a module's channel",
        request=SubjectResolveRequest,
        responses={200: SubjectResolveResponse, 400: None, 404: None},
    )
    def resolve_subjects(self, request: Request) -> Response:
        try:
            data = SubjectResolveRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            result = catalog_service.resolve_subjects(target_module=data.module, channel=data.channel, refs=data.refs)
        except NotSupportedError:
            return _not_supported("This module cannot look up its items.")
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(SubjectResolveResponse(**result).model_dump(mode="json"))
