# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — company profile (contract A4 § 2): data per scope, versions, restore, status, schema.

Thin layer: parse → `profile_service` → Response. ZERO Django models imported. `?channel=` picks the scope
(blank = the instance). Schema violations → 400 with one detail per path (`data.brand.differentiators.0`).
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pydantic import ValidationError
from rest_framework import exceptions, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.profile import PROFILE_SCHEMA, SCHEMA_VERSION
from django_enrichment.schemas.requests import ProfileUpdateRequest
from django_enrichment.schemas.responses import (
    ProfileResponse,
    ProfileSchemaResponse,
    ProfileStatusResponse,
    ProfileVersionResponse,
)
from django_enrichment.services import profile_service
from django_enrichment.services.profile_service import ProfileValidationError

_TAGS = ["Enrichment Profile"]
PROFILE_AREA = "enrichment.profile"
_CHANNEL = OpenApiParameter("channel", str, OpenApiParameter.QUERY, required=False, description="Blank = instance")
MAX_CHANNEL = 64


def _channel(request: Request) -> str:
    channel = request.query_params.get("channel", "")
    if len(channel) > MAX_CHANNEL:
        raise exceptions.ValidationError({"channel": [f"at most {MAX_CHANNEL} characters"]})
    return channel


def profile_body(profile) -> dict:
    return ProfileResponse.model_validate(profile).model_dump(mode="json")


def raise_schema_errors(exc: ProfileValidationError):
    detail: dict[str, list[str]] = {}
    for error in exc.errors:
        path = "data" if error["path"] == "data" else f"data.{error['path']}"
        detail.setdefault(path, []).append(error["message"])
    raise exceptions.ValidationError(detail) from exc


class ProfileViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = PROFILE_AREA
    serializer_class = None

    @extend_schema(
        tags=_TAGS, summary="Retrieve a profile scope", parameters=[_CHANNEL], responses={200: ProfileResponse}
    )
    def retrieve(self, request: Request) -> Response:
        return Response(profile_body(profile_service.get_profile(_channel(request))))

    @extend_schema(
        tags=_TAGS,
        summary="Replace a profile scope's data (new version)",
        parameters=[_CHANNEL],
        request=ProfileUpdateRequest,
        responses={200: ProfileResponse, 400: None},
    )
    def update(self, request: Request) -> Response:
        try:
            data = ProfileUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            profile = profile_service.save_profile(
                channel=_channel(request), data=data.data, note=data.note, user=request.user
            )
        except ProfileValidationError as exc:
            raise_schema_errors(exc)
        return Response(profile_body(profile))

    @extend_schema(
        tags=_TAGS, summary="List a scope's versions", parameters=[_CHANNEL], responses={200: ProfileVersionResponse}
    )
    def versions(self, request: Request) -> Response:
        versions = profile_service.list_versions(_channel(request))
        return Response([ProfileVersionResponse.model_validate(v).model_dump(mode="json") for v in versions])

    @extend_schema(
        tags=_TAGS,
        summary="Restore version n as a new version",
        parameters=[_CHANNEL],
        request=None,
        responses={200: ProfileResponse, 400: None, 404: None},
    )
    def restore(self, request: Request, number: int) -> Response:
        try:
            profile = profile_service.restore(_channel(request), number, user=request.user)
        except ProfileValidationError as exc:
            raise_schema_errors(exc)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(profile_body(profile))

    @extend_schema(
        tags=_TAGS, summary="Profile completeness", parameters=[_CHANNEL], responses={200: ProfileStatusResponse}
    )
    def completeness(self, request: Request) -> Response:
        return Response(ProfileStatusResponse(**profile_service.status(_channel(request))).model_dump())

    @extend_schema(tags=_TAGS, summary="The profile's JSON Schema", responses={200: ProfileSchemaResponse})
    def json_schema(self, request: Request) -> Response:
        return Response(ProfileSchemaResponse(schema_version=SCHEMA_VERSION, json_schema=PROFILE_SCHEMA).model_dump())
