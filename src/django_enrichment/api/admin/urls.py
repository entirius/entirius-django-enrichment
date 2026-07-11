# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API URL routing — manual `path()` per Volkanos convention."""

from django.urls import path

from django_enrichment.api.admin.views.proposal_views import ProposalViewSet
from django_enrichment.api.admin.views.spawn_rule_views import SpawnRuleViewSet
from django_enrichment.api.admin.views.task_views import TaskViewSet

urlpatterns = [
    # Spawn rules (etap-13)
    path("spawn-rules/", SpawnRuleViewSet.as_view({"get": "list", "post": "create"}), name="admin-spawn-rules-list"),
    path(
        "spawn-rules/<slug:key>/",
        SpawnRuleViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-spawn-rules-detail",
    ),
    path("spawn-rules/<slug:key>/run/", SpawnRuleViewSet.as_view({"post": "run"}), name="admin-spawn-rules-run"),
    # Tasks
    path("tasks/", TaskViewSet.as_view({"get": "list", "post": "create"}), name="admin-tasks-list"),
    path("tasks/import-csv/", TaskViewSet.as_view({"post": "import_csv"}), name="admin-tasks-import-csv"),
    path(
        "tasks/<int:pk>/",
        TaskViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="admin-tasks-detail",
    ),
    path("tasks/<int:pk>/targets/", TaskViewSet.as_view({"get": "targets"}), name="admin-tasks-targets"),
    # Proposals
    path("proposals/", ProposalViewSet.as_view({"get": "list", "post": "create"}), name="admin-proposals-list"),
    path(
        "proposals/upload-media/",
        ProposalViewSet.as_view({"post": "upload_media"}),
        name="admin-proposals-upload-media",
    ),
    path("proposals/<int:pk>/", ProposalViewSet.as_view({"get": "retrieve"}), name="admin-proposals-detail"),
    path(
        "proposals/<int:pk>/staged-file/",
        ProposalViewSet.as_view({"get": "staged_file"}),
        name="admin-proposals-staged-file",
    ),
    path("proposals/<int:pk>/accept/", ProposalViewSet.as_view({"post": "accept"}), name="admin-proposals-accept"),
    path("proposals/<int:pk>/reject/", ProposalViewSet.as_view({"post": "reject"}), name="admin-proposals-reject"),
    path(
        "proposals/bulk-accept/", ProposalViewSet.as_view({"post": "bulk_accept"}), name="admin-proposals-bulk-accept"
    ),
    path(
        "proposals/bulk-reject/", ProposalViewSet.as_view({"post": "bulk_reject"}), name="admin-proposals-bulk-reject"
    ),
    path("proposals/bulk-undo/", ProposalViewSet.as_view({"post": "bulk_undo"}), name="admin-proposals-bulk-undo"),
]
