"""Tasks that ran before the cost ledger (0008) may have paid calls nobody recorded: their cost is unknown."""

from django.db import migrations
from django.db.models import Q


def mark_pre_ledger_costs_unknown(apps, schema_editor):
    task_model = apps.get_model("django_enrichment", "EnrichmentTask")
    ran = ~Q(status="open") | Q(proposals__source__startswith="worker:")
    task_model.objects.filter(ran, cost_usd__isnull=True).update(cost_complete=False)


class Migration(migrations.Migration):
    dependencies = [
        ("django_enrichment", "0008_cost_ledger"),
    ]

    operations = [
        migrations.RunPython(mark_pre_ledger_costs_unknown, migrations.RunPython.noop),
    ]
