# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — task templates (contract A3 § 4): list / create / retrieve / patch / delete.

Thin layer: parse → `template_service` → Response. ZERO Django models imported. The list is plain
(a handful of templates per module, ordered by `position`), inactive ones included for the editor.
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pydantic import ValidationError
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.requests import TemplateCreateRequest, TemplateUpdateRequest
from django_enrichment.schemas.responses import TemplateResponse
from django_enrichment.services import template_service

_TAGS = ["Enrichment Templates"]


def _serialize(template) -> dict:
    return TemplateResponse(**template_service.describe(template)).model_dump(mode="json")


def _get_or_404(key: str):
    try:
        return template_service.get_template(key)
    except ValueError as exc:
        raise_as_drf(exc)


def _parse(schema, data):
    try:
        return schema(**data)
    except ValidationError as exc:
        raise_pydantic_as_drf(exc)


class TemplateViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="List task templates",
        parameters=[OpenApiParameter("target_module", str, OpenApiParameter.QUERY, required=False)],
        responses={200: TemplateResponse},
    )
    def list(self, request: Request) -> Response:
        templates = template_service.list_templates(request.query_params.get("target_module"))
        return Response([_serialize(template) for template in templates])

    @extend_schema(tags=_TAGS, summary="Retrieve task template", responses={200: TemplateResponse, 404: None})
    def retrieve(self, request: Request, key: str) -> Response:
        return Response(_serialize(_get_or_404(key)))

    @extend_schema(
        tags=_TAGS,
        summary="Create task template",
        request=TemplateCreateRequest,
        responses={201: TemplateResponse, 400: None},
    )
    def create(self, request: Request) -> Response:
        data = _parse(TemplateCreateRequest, request.data)
        try:
            template = template_service.create_template(**data.model_dump())
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(template), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Update task template",
        request=TemplateUpdateRequest,
        responses={200: TemplateResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, key: str) -> Response:
        data = _parse(TemplateUpdateRequest, request.data)
        template = _get_or_404(key)
        try:
            template = template_service.update_template(template, data.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(template))

    @extend_schema(tags=_TAGS, summary="Delete task template", responses={204: None, 404: None})
    def destroy(self, request: Request, key: str) -> Response:
        template_service.delete_template(_get_or_404(key))
        return Response(status=status.HTTP_204_NO_CONTENT)
