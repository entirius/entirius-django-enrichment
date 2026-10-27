# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import migrations, models


class Migration(migrations.Migration):
    replaces = [
        ("django_enrichment", "0005_qa_check"),
        ("django_enrichment", "0006_apply_mode"),
    ]

    dependencies = [
        ("django_enrichment", "0004_ai_studio"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentproposal",
            name="standard_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="qa_check",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="contentproposal",
            name="apply_mode",
            field=models.CharField(
                blank=True, choices=[("add", "Add"), ("replace", "Replace")], default="", max_length=16
            ),
        ),
    ]
