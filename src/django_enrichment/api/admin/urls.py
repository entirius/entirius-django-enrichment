# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API URL routing — manual `path()` per Volkanos convention."""

from django.urls import path

from django_enrichment.api.admin.views.catalog_views import CatalogViewSet
from django_enrichment.api.admin.views.memory_views import MemoryViewSet
from django_enrichment.api.admin.views.profile_views import ProfileViewSet
from django_enrichment.api.admin.views.proposal_views import ProposalViewSet
from django_enrichment.api.admin.views.queue_views import WorkQueueViewSet
from django_enrichment.api.admin.views.revision_views import RevisionViewSet, RulePromotionViewSet
from django_enrichment.api.admin.views.spawn_rule_views import SpawnRuleViewSet
from django_enrichment.api.admin.views.task_views import TaskViewSet
from django_enrichment.api.admin.views.template_views import TemplateViewSet
from django_enrichment.api.admin.views.usage_views import AIUsageViewSet
from django_enrichment.api.admin.views.worker_views import WorkerViewSet
from django_enrichment.api.admin.views.workflow_views import RunViewSet, WorkflowViewSet

urlpatterns = [
    # Spawn rules (etap-13)
    path("spawn-rules/", SpawnRuleViewSet.as_view({"get": "list", "post": "create"}), name="admin-spawn-rules-list"),
    path(
        "spawn-rules/<slug:key>/",
        SpawnRuleViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-spawn-rules-detail",
    ),
    path("spawn-rules/<slug:key>/run/", SpawnRuleViewSet.as_view({"post": "run"}), name="admin-spawn-rules-run"),
    # AI usage this month (CMS header counter)
    path("usage/month/", AIUsageViewSet.as_view({"get": "month"}), name="admin-usage-month"),
    # Workers
    path("workers/", WorkerViewSet.as_view({"get": "list"}), name="admin-workers-list"),
    path("workers/status/", WorkerViewSet.as_view({"get": "status"}), name="admin-workers-status"),
    path("workers/<str:key>/resume/", WorkerViewSet.as_view({"post": "resume"}), name="admin-workers-resume"),
    # Work queue: the pull model of external and internal workers
    path("workers/claim/", WorkQueueViewSet.as_view({"post": "claim"}), name="admin-queue-claim"),
    path("workers/heartbeat/", WorkQueueViewSet.as_view({"post": "heartbeat"}), name="admin-queue-heartbeat"),
    path("workers/complete/", WorkQueueViewSet.as_view({"post": "complete"}), name="admin-queue-complete"),
    path("workers/fail/", WorkQueueViewSet.as_view({"post": "fail"}), name="admin-queue-fail"),
    # Templates & field catalog (contract A3 § 2, § 4)
    path("templates/", TemplateViewSet.as_view({"get": "list", "post": "create"}), name="admin-templates-list"),
    path(
        "templates/<str:key>/",
        TemplateViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-templates-detail",
    ),
    path("fields/", CatalogViewSet.as_view({"get": "fields"}), name="admin-fields"),
    path("languages/", CatalogViewSet.as_view({"get": "languages"}), name="admin-languages"),
    path(
        "subjects/resolve/",
        CatalogViewSet.as_view({"post": "resolve_subjects"}),
        name="admin-subjects-resolve",
    ),
    path("subjects/pictures/", CatalogViewSet.as_view({"get": "subject_pictures"}), name="admin-subjects-pictures"),
    # Workflows (contract A4 § 6)
    path("workflows/", WorkflowViewSet.as_view({"get": "list"}), name="admin-workflows-list"),
    path("runs/", RunViewSet.as_view({"get": "list", "post": "create"}), name="admin-runs-list"),
    path("runs/<str:uid>/", RunViewSet.as_view({"get": "retrieve"}), name="admin-runs-detail"),
    path(
        "runs/<str:uid>/steps/<str:key>/regenerate/",
        RunViewSet.as_view({"post": "regenerate"}),
        name="admin-runs-step-regenerate",
    ),
    path("runs/<str:uid>/cancel/", RunViewSet.as_view({"post": "cancel"}), name="admin-runs-cancel"),
    # Company profile (contract A4 § 2)
    path("profile/", ProfileViewSet.as_view({"get": "retrieve", "put": "update"}), name="admin-profile"),
    path("profile/versions/", ProfileViewSet.as_view({"get": "versions"}), name="admin-profile-versions"),
    path(
        "profile/versions/<int:number>/restore/",
        ProfileViewSet.as_view({"post": "restore"}),
        name="admin-profile-version-restore",
    ),
    path("profile/status/", ProfileViewSet.as_view({"get": "completeness"}), name="admin-profile-status"),
    path("profile/schema/", ProfileViewSet.as_view({"get": "json_schema"}), name="admin-profile-schema"),
    path(
        "profile/settings/",
        ProfileViewSet.as_view({"get": "ai_settings", "put": "update_ai_settings"}),
        name="admin-profile-settings",
    ),
    # Memory (contract A4 § 3)
    path("memory/", MemoryViewSet.as_view({"get": "list"}), name="admin-memory-list"),
    path(
        "memory/<int:pk>/",
        MemoryViewSet.as_view({"patch": "partial_update", "delete": "destroy"}),
        name="admin-memory-detail",
    ),
    path("memory/<int:pk>/promote/", MemoryViewSet.as_view({"post": "promote"}), name="admin-memory-promote"),
    # Tasks
    path("tasks/", TaskViewSet.as_view({"get": "list", "post": "create"}), name="admin-tasks-list"),
    path("tasks/import-csv/", TaskViewSet.as_view({"post": "import_csv"}), name="admin-tasks-import-csv"),
    path(
        "tasks/<int:pk>/",
        TaskViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="admin-tasks-detail",
    ),
    path("tasks/<int:pk>/revise/", RevisionViewSet.as_view({"post": "revise_task"}), name="admin-tasks-revise"),
    path("tasks/<int:pk>/retry/", TaskViewSet.as_view({"post": "retry"}), name="admin-tasks-retry"),
    path("tasks/<int:pk>/targets/", TaskViewSet.as_view({"get": "targets"}), name="admin-tasks-targets"),
    # Proposals
    path("proposals/", ProposalViewSet.as_view({"get": "list", "post": "create"}), name="admin-proposals-list"),
    path(
        "proposals/upload-media/",
        ProposalViewSet.as_view({"post": "upload_media"}),
        name="admin-proposals-upload-media",
    ),
    path(
        "proposals/<int:pk>/",
        ProposalViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="admin-proposals-detail",
    ),
    path(
        "proposals/<int:pk>/staged-file/",
        ProposalViewSet.as_view({"get": "staged_file"}),
        name="admin-proposals-staged-file",
    ),
    # Revisions & corrections (contract A3 § 1)
    path(
        "proposals/<int:pk>/revisions/",
        RevisionViewSet.as_view({"get": "revisions"}),
        name="admin-proposals-revisions",
    ),
    path(
        "proposals/<int:pk>/revisions/<int:number>/restore/",
        RevisionViewSet.as_view({"post": "restore"}),
        name="admin-proposals-revision-restore",
    ),
    path("proposals/<int:pk>/revise/", RevisionViewSet.as_view({"post": "revise"}), name="admin-proposals-revise"),
    path("proposals/<int:pk>/rules/", RevisionViewSet.as_view({"put": "rules"}), name="admin-proposals-rules"),
    # QA checks (contract A4 § 4)
    path("proposals/<int:pk>/check/", RevisionViewSet.as_view({"post": "qa_check"}), name="admin-proposals-check"),
    path(
        "proposals/<int:pk>/facts/suggest/",
        RevisionViewSet.as_view({"post": "suggest_fact"}),
        name="admin-proposals-facts-suggest",
    ),
    path(
        "proposals/<int:pk>/rules/promote/",
        RulePromotionViewSet.as_view({"post": "promote_rule"}),
        name="admin-proposals-rules-promote",
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
