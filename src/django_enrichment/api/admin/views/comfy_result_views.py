# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — what the `comfy` workers found (`ComfyResult`): the list with score and thumbnail, verdict totals.

Read-only: results are written by the kind only. Thin layer: ZERO Django models imported.
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pydantic import ValidationError
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.pagination import AdminPageNumberPagination
from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.requests import ComfyResultsQuery
from django_enrichment.schemas.responses import (
    ComfyResultListResponse,
    ComfyResultResponse,
    ComfyVerdictTotalsResponse,
)
from django_enrichment.services import comfy_results_query

_TAGS = ["Enrichment Comfy Results"]
_PARAMS = [
    OpenApiParameter(name, int if name == "task" else str, OpenApiParameter.QUERY, required=False)
    for name in ComfyResultsQuery.model_fields
]


def _query(request: Request) -> ComfyResultsQuery:
    params = {key: value for key, value in request.query_params.dict().items() if key in ComfyResultsQuery.model_fields}
    try:
        return ComfyResultsQuery(**params)
    except ValidationError as exc:
        raise_pydantic_as_drf(exc)


def _filter(query: ComfyResultsQuery) -> comfy_results_query.ResultFilter:
    return comfy_results_query.ResultFilter(**query.model_dump(exclude={"ordering"}))


class ComfyResultViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="List comfy results",
        parameters=_PARAMS,
        responses={200: ComfyResultListResponse, 400: None},
    )
    def list(self, request: Request) -> Response:
        query = _query(request)
        try:
            rows = comfy_results_query.list_results(_filter(query), query.ordering)
        except ValueError as exc:
            raise_as_drf(exc)
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(rows, request)
        items = [ComfyResultResponse(**row).model_dump(mode="json") for row in comfy_results_query.describe(page)]
        return paginator.get_paginated_response(items)

    @extend_schema(
        tags=_TAGS,
        summary="Comfy results per verdict",
        parameters=_PARAMS,
        responses={200: ComfyVerdictTotalsResponse, 400: None},
    )
    def verdicts(self, request: Request) -> Response:
        totals = comfy_results_query.verdict_totals(_filter(_query(request)))
        body = ComfyVerdictTotalsResponse(total=sum(totals.values()), verdicts=totals)
        return Response(body.model_dump(mode="json"))
