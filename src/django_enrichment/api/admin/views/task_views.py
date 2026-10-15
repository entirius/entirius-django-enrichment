# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — EnrichmentTask (spawn / list / retrieve / targets / status).

Thin layer: parse → service → Response. ZERO Django models imported here — all ORM goes through
`task_service`. The worker pull-loop is: GET /tasks/?status=open → GET /tasks/{id}/targets/ →
POST /proposals/ → PATCH /tasks/{id}/ (done).
"""

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
from django_enrichment.api.admin.throttling import SpawnThrottle
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.requests import TaskCSVImportRequest, TaskSpawnRequest, TaskUpdateRequest
from django_enrichment.schemas.responses import (
    CSVImportResponse,
    TargetsResponse,
    TaskListResponse,
    TaskResponse,
    TaskSpawnResponse,
)
from django_enrichment.services import lane_service, run_service, task_service

_TAGS = ["Enrichment Tasks"]


def _serialize(task) -> dict:
    return TaskResponse.model_validate(task).model_dump(mode="json")


def _serialize_with_proposals(tasks: list) -> list[dict]:
    """Tasks plus the status counts of their proposals (what the operator still has to review) and queue position."""
    counts = task_service.proposal_counts([t.pk for t in tasks])
    ahead = lane_service.queued_ahead(tasks)
    return [{**_serialize(t), "proposal_counts": counts[t.pk], "queued_ahead": ahead[t.pk]} for t in tasks]


class AlreadyRunning(drf_exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "already_running"


def _get_task_or_404(task_id: int):
    try:
        return task_service.get_task(task_id)
    except ValueError as exc:
        raise_as_drf(exc)


class TaskViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    # Multipart is needed for `import_csv`; JSON/form keep every other action working as before.
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    serializer_class = None

    def get_throttles(self):
        # Task spawning (single spawn + CSV import) is the analyzer/ops traffic path — capped so a
        # leaked service-account token or a runaway loop can't spam it. Reads/status-PATCH are not.
        if self.action in ("create", "import_csv"):
            return [SpawnThrottle()]
        return []

    @extend_schema(
        tags=_TAGS,
        summary="List enrichment tasks",
        parameters=[
            OpenApiParameter("type", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter(
                "status", str, OpenApiParameter.QUERY, required=False, description="one status or a comma list"
            ),
            OpenApiParameter("batch_key", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter(
                "subject_ref", str, OpenApiParameter.QUERY, required=False, description="list-mode tasks naming it"
            ),
            OpenApiParameter("target_module", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: TaskListResponse},
    )
    def list(self, request: Request) -> Response:
        qp = request.query_params
        status_param = qp.get("status")
        qs = task_service.list_tasks(
            task_service.TaskFilters(
                type=qp.get("type"),
                statuses=status_param.split(",") if status_param else None,
                batch_key=qp.get("batch_key"),
                subject_ref=qp.get("subject_ref"),
                target_module=qp.get("target_module"),
            )
        )
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(_serialize_with_proposals(list(page)))

    @extend_schema(tags=_TAGS, summary="Retrieve task", responses={200: TaskResponse, 404: None})
    def retrieve(self, request: Request, pk: int) -> Response:
        return Response(_serialize_with_proposals([_get_task_or_404(pk)])[0])

    @extend_schema(
        tags=_TAGS,
        summary="Spawn task",
        request=TaskSpawnRequest,
        responses={201: TaskSpawnResponse, 400: None, 409: None},
    )
    def create(self, request: Request) -> Response:
        try:
            data = TaskSpawnRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            result = run_service.spawn(data, requested_by=request.user)
        except run_service.AlreadyRunningError as exc:
            raise AlreadyRunning(str(exc)) from exc
        except ValueError as exc:
            raise_as_drf(exc)
        body = {**_serialize_with_proposals([result.task])[0], "skipped_in_flight": result.skipped_in_flight}
        return Response(TaskSpawnResponse.model_validate(body).model_dump(mode="json"), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Import a CSV → N tasks (multipart)",
        request=TaskCSVImportRequest,
        responses={201: CSVImportResponse, 400: None},
    )
    def import_csv(self, request: Request) -> Response:
        """Spawn N single-type tasks from one uploaded CSV (`sku,field,type`), grouped by work-type.

        The bus stages the file and streams targets lazily at resolve time (the adapter never reads
        it — decision D1). One file = one channel + one language; rows are grouped by their `type`
        column (the work-type → `task.type`)."""
        upload = request.FILES.get("file")
        if upload is None:
            raise drf_exceptions.ValidationError("file is required")
        try:
            data = TaskCSVImportRequest(**{k: v for k, v in request.data.items() if k != "file"})
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            tasks = task_service.import_csv(
                file=upload,
                module=data.module,
                channel=data.channel,
                language=data.language,
                params=data.params,
                requested_by=request.user,
            )
        except ValueError as exc:
            raise_as_drf(exc)
        staged_ref = tasks[0].scope_spec["file"] if tasks else ""
        body = CSVImportResponse(
            staged_file=staged_ref, task_count=len(tasks), tasks=[TaskResponse.model_validate(t) for t in tasks]
        )
        return Response(body.model_dump(mode="json"), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Resolve task targets (worker pull)",
        parameters=[OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False)],
        responses={200: TargetsResponse, 404: None},
    )
    def targets(self, request: Request, pk: int) -> Response:
        task = _get_task_or_404(pk)
        # Validate at the boundary — a non-numeric/negative page must be a 400, not a 500 (and for a
        # CSV task a huge page would otherwise scan the staged file to nowhere).
        raw_page = request.query_params.get("page", "1")
        try:
            page = int(raw_page)
        except (TypeError, ValueError) as exc:
            raise drf_exceptions.ValidationError("page must be a positive integer") from exc
        if page < 1:
            raise drf_exceptions.ValidationError("page must be a positive integer")
        try:
            resolved = task_service.resolve_targets(task, page=page)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(TargetsResponse(task_id=task.id, page=page, targets=resolved).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Update task status",
        request=TaskUpdateRequest,
        responses={200: TaskResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, pk: int) -> Response:
        try:
            data = TaskUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        task = _get_task_or_404(pk)
        try:
            task = task_service.transition_status(task, data.status, user=request.user)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(task))
