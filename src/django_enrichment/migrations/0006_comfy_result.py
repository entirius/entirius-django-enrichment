# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_enrichment", "0005_ai_studio_media"),
    ]

    operations = [
        migrations.CreateModel(
            name="ComfyResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("modified_at", models.DateTimeField(auto_now=True)),
                ("input_sha1", models.CharField(max_length=40)),
                ("worker_key", models.CharField(max_length=64)),
                ("workflow_hash", models.CharField(max_length=64)),
                ("subject_ref", models.CharField(max_length=255)),
                ("source", models.CharField(max_length=16)),
                ("metrics", models.JSONField(blank=True, default=dict)),
                ("verdict", models.CharField(max_length=32)),
                ("reason", models.CharField(blank=True, default="", max_length=64)),
                ("worker_version", models.PositiveIntegerField()),
            ],
            options={
                "indexes": [
                    models.Index(fields=["input_sha1", "worker_key", "workflow_hash"], name="enr_comfy_file_idx"),
                    models.Index(fields=["worker_key", "verdict"], name="enr_comfy_worker_verdict_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("subject_ref", "input_sha1", "worker_key", "workflow_hash"), name="enr_comfy_result_key"
                    )
                ],
            },
        ),
    ]
