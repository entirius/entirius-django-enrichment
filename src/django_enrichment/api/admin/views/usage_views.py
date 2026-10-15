# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API — this month's AI usage (the CMS header counter). Thin layer: ZERO Django models imported.

Disabled / not configured toolbox keeps its own 503 codes; any other toolbox failure is one quiet 503
`AI_USAGE_UNAVAILABLE` (logged with type and code, never the toolbox message) — the counter is display only.
"""

import logging

from django_utils.toolbox import ToolboxDisabledError, ToolboxError, ToolboxNotConfiguredError, handle_toolbox_error
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_enrichment.api.admin.permissions import IsAdminUser
from django_enrichment.schemas.responses import AIUsageMonthResponse
from django_enrichment.services import ai_usage_service

logger = logging.getLogger(__name__)

_TAGS = ["Enrichment AI usage"]


class AIUsageViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    access_area = "enrichment.proposals"
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="AI usage this month",
        description="Tokens and billed cost of this instance's AI toolbox client in the current UTC month "
        "(cached 60 s).",
        responses={
            200: AIUsageMonthResponse,
            503: OpenApiResponse(description="AI_USAGE_UNAVAILABLE, AI_TOOLBOX_DISABLED or AI_TOOLBOX_NOT_CONFIGURED"),
        },
    )
    def month(self, request: Request) -> Response:
        try:
            usage = ai_usage_service.current_month()
        except (ToolboxDisabledError, ToolboxNotConfiguredError) as exc:
            return handle_toolbox_error(exc)
        except ToolboxError as exc:
            return _unavailable(exc)
        return Response(AIUsageMonthResponse(**usage).model_dump(mode="json"))


def _unavailable(exc: ToolboxError) -> Response:
    logger.warning("AI usage unavailable (type=%s, status=%d, code=%s)", type(exc).__name__, exc.status_code, exc.code)
    return Response(
        {"error": "AI_USAGE_UNAVAILABLE", "message": "AI usage is unavailable right now."},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
