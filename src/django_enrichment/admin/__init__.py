from django_enrichment.admin.content_proposal import ContentProposalAdmin
from django_enrichment.admin.enrichment_task import EnrichmentTaskAdmin
from django_enrichment.admin.spawn_rule import SpawnRuleAdmin
from django_enrichment.admin.task_template import TaskTemplateAdmin
from django_enrichment.admin.worker import WorkerAdmin
from django_enrichment.admin.workflow import WorkflowDefinitionAdmin, WorkflowRunAdmin

__all__ = [
    "ContentProposalAdmin",
    "EnrichmentTaskAdmin",
    "SpawnRuleAdmin",
    "TaskTemplateAdmin",
    "WorkerAdmin",
    "WorkflowDefinitionAdmin",
    "WorkflowRunAdmin",
]
