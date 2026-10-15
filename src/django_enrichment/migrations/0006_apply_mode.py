# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_enrichment", "0005_qa_check"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentproposal",
            name="apply_mode",
            field=models.CharField(
                blank=True, choices=[("add", "Add"), ("replace", "Replace")], default="", max_length=16
            ),
        ),
    ]
