# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — Worker (read-only list for the CMS spawn dialog).

Workers are configured in Django admin only. The CMS reads the active ones to offer task types
and render the params form from `params_schema`. Thin layer: ZERO Django models imported.
"""

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.responses import WorkerResponse, WorkerStatusResponse
from django_enrichment.services import queue_status, worker_service

_TAGS = ["Enrichment Workers"]


class WorkerViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="List active workers",
        parameters=[OpenApiParameter("target_module", str, OpenApiParameter.QUERY, required=False)],
        responses={200: WorkerResponse},
    )
    def list(self, request: Request) -> Response:
        workers = worker_service.list_active(request.query_params.get("target_module"))
        return Response(
            [WorkerResponse(**worker_service.describe(worker)).model_dump(mode="json") for worker in workers]
        )

    @extend_schema(
        tags=_TAGS,
        summary="Queue state of every active worker",
        responses={200: WorkerStatusResponse},
    )
    def status(self, request: Request) -> Response:
        rows = queue_status.workers_status()
        return Response([WorkerStatusResponse(**row).model_dump(mode="json") for row in rows])

    @extend_schema(
        tags=_TAGS, summary="Resume a paused worker", request=None, responses={200: WorkerStatusResponse, 404: None}
    )
    def resume(self, request: Request, key: str) -> Response:
        try:
            queue_status.resume(key)
        except ValueError as exc:
            raise_as_drf(exc)
        row = next(r for r in queue_status.workers_status() if r["key"] == key)
        return Response(WorkerStatusResponse(**row).model_dump(mode="json"))
