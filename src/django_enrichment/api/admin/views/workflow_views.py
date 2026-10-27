# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — workflows and their runs (contract A4 § 6).

Thin layer: parse → `workflow_service` → Response. ZERO Django models imported. A step is approved by
accepting its proposal (`POST proposals/{id}/accept/`), so runs have no approve endpoint of their own.
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
from django_enrichment.api.admin.throttling import SpawnThrottle
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.requests import RunStartRequest, StepRegenerateRequest
from django_enrichment.schemas.responses import RunListResponse, RunResponse, WorkflowDefinitionResponse
from django_enrichment.services import workflow_queries, workflow_service

_TAGS = ["Enrichment Workflows"]


def _run(run) -> dict:
    return RunResponse(**workflow_queries.describe(run)).model_dump(mode="json")


def _parse(schema, data):
    try:
        return schema(**data)
    except ValidationError as exc:
        raise_pydantic_as_drf(exc)


def _call(fn, *args, **kwargs):
    """Service call with `ValueError` → 404 (`... not found`) or 400."""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise_as_drf(exc)


class WorkflowViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    serializer_class = None

    @extend_schema(tags=_TAGS, summary="List active workflows", responses={200: WorkflowDefinitionResponse})
    def list(self, request: Request) -> Response:
        definitions = workflow_queries.list_definitions()
        return Response(
            [WorkflowDefinitionResponse(**workflow_queries.describe_definition(d)).model_dump() for d in definitions]
        )


class RunViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    serializer_class = None

    def get_throttles(self):
        # Starting and regenerating spawn paid toolbox work.
        return [SpawnThrottle()] if self.action in ("create", "regenerate") else []

    @extend_schema(
        tags=_TAGS,
        summary="List workflow runs",
        parameters=[
            OpenApiParameter("status", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("definition", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: RunListResponse},
    )
    def list(self, request: Request) -> Response:
        qp = request.query_params
        runs = workflow_queries.list_runs(status=qp.get("status"), definition=qp.get("definition"))
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(runs, request)
        return paginator.get_paginated_response([_run(run) for run in page])

    @extend_schema(
        tags=_TAGS, summary="Start a workflow run", request=RunStartRequest, responses={201: RunResponse, 400: None}
    )
    def create(self, request: Request) -> Response:
        data = _parse(RunStartRequest, request.data)
        run = _call(workflow_service.start, **data.model_dump(), user=request.user)
        return Response(_run(run), status=status.HTTP_201_CREATED)

    @extend_schema(tags=_TAGS, summary="Retrieve a workflow run", responses={200: RunResponse, 404: None})
    def retrieve(self, request: Request, uid: str) -> Response:
        return Response(_run(_call(workflow_queries.get_run, uid)))

    @extend_schema(
        tags=_TAGS,
        summary="Regenerate a step (in review or failed)",
        request=StepRegenerateRequest,
        responses={200: RunResponse, 400: None, 404: None},
    )
    def regenerate(self, request: Request, uid: str, key: str) -> Response:
        data = _parse(StepRegenerateRequest, request.data)
        return Response(_run(_call(workflow_service.regenerate, uid, key, instruction=data.instruction)))

    @extend_schema(tags=_TAGS, summary="Cancel a workflow run", responses={200: RunResponse, 400: None, 404: None})
    def cancel(self, request: Request, uid: str) -> Response:
        return Response(_run(_call(workflow_service.cancel, uid, user=request.user)))
