# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — SpawnRule (CRUD + run, etap-13).

Thin layer: parse → service → Response. ZERO Django models imported here — all ORM goes through
`spawn_rule_service`. `run` is the CMS "Run now" button; it shares the spawn throttle with the
task surfaces (same abuse profile: each run may create a task).
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
from django_enrichment.schemas.requests import SpawnRuleCreateRequest, SpawnRuleUpdateRequest
from django_enrichment.schemas.responses import (
    SpawnRuleListResponse,
    SpawnRuleResponse,
    SpawnRuleRunResponse,
    TaskResponse,
)
from django_enrichment.services import spawn_rule_service

_TAGS = ["Enrichment Spawn Rules"]


def _serialize(rule) -> dict:
    return SpawnRuleResponse.model_validate(rule).model_dump(mode="json")


def _get_rule_or_404(key: str):
    try:
        return spawn_rule_service.get_rule(key)
    except ValueError as exc:
        raise_as_drf(exc)


class SpawnRuleViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    serializer_class = None

    def get_throttles(self):
        if self.action == "run":
            return [SpawnThrottle()]
        return []

    @extend_schema(
        tags=_TAGS,
        summary="List spawn rules",
        parameters=[
            OpenApiParameter("active", bool, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("search", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: SpawnRuleListResponse},
    )
    def list(self, request: Request) -> Response:
        active_param = request.query_params.get("active")
        active = None if active_param is None else active_param.lower() in ("true", "1", "yes")
        qs = spawn_rule_service.list_rules(active=active, search=request.query_params.get("search"))
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response([_serialize(r) for r in page])

    @extend_schema(tags=_TAGS, summary="Retrieve spawn rule", responses={200: SpawnRuleResponse, 404: None})
    def retrieve(self, request: Request, key: str) -> Response:
        return Response(_serialize(_get_rule_or_404(key)))

    @extend_schema(
        tags=_TAGS,
        summary="Create spawn rule",
        request=SpawnRuleCreateRequest,
        responses={201: SpawnRuleResponse, 400: None},
    )
    def create(self, request: Request) -> Response:
        try:
            data = SpawnRuleCreateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            rule = spawn_rule_service.create_rule(**data.model_dump())
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(rule), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Update spawn rule",
        request=SpawnRuleUpdateRequest,
        responses={200: SpawnRuleResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, key: str) -> Response:
        try:
            data = SpawnRuleUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        rule = _get_rule_or_404(key)
        try:
            rule = spawn_rule_service.update_rule(rule, data.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(rule))

    @extend_schema(tags=_TAGS, summary="Delete spawn rule", responses={200: None, 404: None})
    def destroy(self, request: Request, key: str) -> Response:
        rule = _get_rule_or_404(key)
        spawn_rule_service.delete_rule(rule)
        return Response({"detail": f"Spawn rule '{key}' deleted"})

    @extend_schema(
        tags=_TAGS,
        summary="Run a spawn rule now",
        request=None,
        responses={200: SpawnRuleRunResponse, 201: SpawnRuleRunResponse, 404: None},
    )
    def run(self, request: Request, key: str) -> Response:
        rule = _get_rule_or_404(key)
        existing = spawn_rule_service.find_running_task(rule.key)
        if existing is not None:
            body = SpawnRuleRunResponse(status="already_running", task=TaskResponse.model_validate(existing))
            return Response(body.model_dump(mode="json"))
        try:
            task = spawn_rule_service.run_rule(rule, requested_by=request.user)
        except ValueError as exc:
            raise_as_drf(exc)  # e.g. unknown check key surfaced by the adapter
        if task is None:
            return Response(SpawnRuleRunResponse(status="no_candidates", task=None).model_dump(mode="json"))
        body = SpawnRuleRunResponse(status="spawned", task=TaskResponse.model_validate(task))
        return Response(body.model_dump(mode="json"), status=status.HTTP_201_CREATED)
