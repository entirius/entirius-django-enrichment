# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — proposal revisions, AI corrections, correction rules, batch correction (contract A3 § 1).

Thin layer: parse → service → Response. ZERO Django models imported. Toolbox errors are mapped by
`django_utils.toolbox.views.handle_toolbox_error`; an answer that cannot be applied is a 422
`REVISION_NOT_APPLICABLE` in the v2 error shape.
"""

import uuid

from django_utils.api.v2_errors import ErrorResponse, raise_pydantic_as_drf
from django_utils.toolbox import ToolboxError
from django_utils.toolbox.views import handle_toolbox_error
from drf_spectacular.utils import extend_schema
from pydantic import ValidationError
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.api.admin.throttling import BulkActionThrottle
from django_enrichment.api.admin.views._helpers import raise_as_drf
from django_enrichment.schemas.requests import ReviseRequest, RulePromoteRequest, RulesUpdateRequest, TaskReviseRequest
from django_enrichment.schemas.responses import (
    ProposalResponse,
    RevisionResponse,
    RulePromoteResponse,
    TaskReviseResponse,
    VariantsResponse,
)
from django_enrichment.services import (
    proposal_service,
    revise_batch_service,
    revise_service,
    revision_service,
    rules_service,
    task_service,
)

_TAGS = ["Enrichment Revisions"]


def _revision(revision) -> dict:
    return RevisionResponse.model_validate(revision).model_dump(mode="json")


def _get_or_404(getter, pk: int):
    try:
        return getter(pk)
    except ValueError as exc:
        raise_as_drf(exc)


def _parse(schema, data):
    try:
        return schema(**data)
    except ValidationError as exc:
        raise_pydantic_as_drf(exc)


def _not_applicable() -> Response:
    body = ErrorResponse(
        error="REVISION_NOT_APPLICABLE",
        message="The correction could not be applied to the current text. Try again or rephrase it.",
        debug_id=uuid.uuid4().hex[:8],
        details=[],
    )
    return Response(body.model_dump(), status=status.HTTP_422_UNPROCESSABLE_ENTITY)


def _revise_response(result) -> Response:
    if isinstance(result, list):
        return Response(VariantsResponse(candidates=result).model_dump(mode="json"))
    return Response(_revision(result), status=status.HTTP_201_CREATED)


class RevisionViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    serializer_class = None

    def get_throttles(self):
        # Corrections are paid toolbox calls — capped like the other mass/expensive actions.
        if self.action in ("revise", "revise_task"):
            return [BulkActionThrottle()]
        return []

    @extend_schema(tags=_TAGS, summary="List a proposal's revisions", responses={200: RevisionResponse, 404: None})
    def revisions(self, request: Request, pk: int) -> Response:
        proposal = _get_or_404(proposal_service.get_proposal, pk)
        return Response([_revision(r) for r in revision_service.list_revisions(proposal)])

    @extend_schema(
        tags=_TAGS,
        summary="AI correction (201 revision, or 200 candidates for `variants`)",
        request=ReviseRequest,
        responses={201: RevisionResponse, 200: VariantsResponse, 400: None, 422: None},
    )
    def revise(self, request: Request, pk: int) -> Response:
        data = _parse(ReviseRequest, request.data)
        proposal = _get_or_404(proposal_service.get_proposal, pk)
        scope = data.scope.model_dump() if data.scope else None
        try:
            result = revise_service.revise(
                proposal, instruction=data.instruction, quick_action=data.quick_action, scope=scope, user=request.user
            )
        except revise_service.RevisionNotApplicableError:
            return _not_applicable()
        except ToolboxError as exc:
            return handle_toolbox_error(exc)
        except ValueError as exc:
            raise_as_drf(exc)
        return _revise_response(result)

    @extend_schema(tags=_TAGS, summary="Restore revision n as a new revision", responses={201: RevisionResponse})
    def restore(self, request: Request, pk: int, number: int) -> Response:
        proposal = _get_or_404(proposal_service.get_proposal, pk)
        try:
            revision = revision_service.restore(proposal, number, user=request.user)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_revision(revision), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS, summary="Replace correction rules", request=RulesUpdateRequest, responses={200: ProposalResponse}
    )
    def rules(self, request: Request, pk: int) -> Response:
        data = _parse(RulesUpdateRequest, request.data)
        proposal = _get_or_404(proposal_service.get_proposal, pk)
        try:
            proposal = rules_service.set_rules(proposal, data.rules)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(ProposalResponse.model_validate(proposal).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Promote a rule to the template or worker",
        request=RulePromoteRequest,
        responses={200: RulePromoteResponse},
    )
    def promote_rule(self, request: Request, pk: int) -> Response:
        data = _parse(RulePromoteRequest, request.data)
        proposal = _get_or_404(proposal_service.get_proposal, pk)
        try:
            rules = rules_service.promote(proposal, data.rule, target=data.target)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(RulePromoteResponse(target=data.target, rules=rules).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Correct all pending proposals of a task (async)",
        request=TaskReviseRequest,
        responses={202: TaskReviseResponse},
    )
    def revise_task(self, request: Request, pk: int) -> Response:
        data = _parse(TaskReviseRequest, request.data)
        task = _get_or_404(task_service.get_task, pk)
        try:
            queued = revise_batch_service.queue(task, data.model_dump(), user=request.user)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(TaskReviseResponse(queued=queued).model_dump(), status=status.HTTP_202_ACCEPTED)
