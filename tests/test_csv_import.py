# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""CSV import spawn (etap-11) — validation, staging, group-by-type = N tasks, lazy streaming.

The CSV is `sku,field,type`; one file = one channel + one language. Rows group by `type` (the
work-type → `task.type`) into N single-type tasks streaming lazily from one staged file. The bus
parses the file itself (`resolve_targets` for `mode:csv`) — the adapter never reads the staging
store (decision D1), so these tests need no `fake_adapter`.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from django_enrichment.enums import TaskStatus
from django_enrichment.services import csv_import, staging_store, task_service

_GOOD = (
    "sku,field,type\nENT-S001,description,fix-attribute\nENT-S002,description,fix-attribute\nENT-S001,name,translate\n"
)


@pytest.fixture
def staging_dir(settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    return tmp_path


def _csv(text: str, name: str = "fields.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


# ── validate_and_stage: header + row shape ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_validate_and_stage_returns_ref_and_sorted_types(staging_dir):
    ref, types = csv_import.validate_and_stage(_csv(_GOOD))

    assert staging_store.exists(ref)
    assert types == ["fix-attribute", "translate"]  # sorted, deduped


def test_validate_rejects_wrong_header(staging_dir):
    with pytest.raises(ValueError, match="header must be exactly"):
        csv_import.validate_and_stage(_csv("sku,feature,kind\nA,b,c\n"))


def test_validate_rejects_missing_column(staging_dir):
    with pytest.raises(ValueError, match="header must be exactly"):
        csv_import.validate_and_stage(_csv("sku,field\nA,b\n"))


def test_validate_rejects_extra_future_column(staging_dir):
    # channel/language columns are future (mixing) — reject now rather than silently change meaning.
    with pytest.raises(ValueError, match="header must be exactly"):
        csv_import.validate_and_stage(_csv("sku,field,type,channel\nA,b,fix,default\n"))


def test_validate_rejects_empty_cell_with_row_number(staging_dir):
    with pytest.raises(ValueError, match="row 3"):
        csv_import.validate_and_stage(_csv("sku,field,type\nA,b,fix\nB,,fix\n"))


def test_validate_rejects_header_only(staging_dir):
    with pytest.raises(ValueError, match="no data rows"):
        csv_import.validate_and_stage(_csv("sku,field,type\n"))


def test_validate_rejects_empty_file(staging_dir):
    with pytest.raises(ValueError, match="empty"):
        csv_import.validate_and_stage(_csv(""))


def test_validate_rejects_oversize(staging_dir, settings):
    settings.ENRICHMENT_MAX_CSV_BYTES = 10
    with pytest.raises(ValueError, match="cap"):
        csv_import.validate_and_stage(_csv(_GOOD))


def test_validate_rejects_non_utf8(staging_dir):
    bad = SimpleUploadedFile("f.csv", b"sku,field,type\n\xff\xfe,b,c\n", content_type="text/csv")
    with pytest.raises(ValueError, match="UTF-8"):
        csv_import.validate_and_stage(bad)


def test_validate_tolerates_utf8_bom(staging_dir):
    ref, types = csv_import.validate_and_stage(_csv("﻿" + _GOOD))
    assert staging_store.exists(ref)
    assert types == ["fix-attribute", "translate"]


# ── import_csv: group by type = N tasks ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_import_csv_groups_by_type_into_n_tasks(staging_dir):
    tasks = task_service.import_csv(file=_csv(_GOOD), module="pim", channel="default", language="pl")

    assert len(tasks) == 2
    assert {t.type for t in tasks} == {"fix-attribute", "translate"}
    refs = {t.scope_spec["file"] for t in tasks}
    assert len(refs) == 1  # one staged file shared by all N tasks
    for t in tasks:
        assert t.status == TaskStatus.OPEN.value
        assert t.scope_spec["mode"] == "csv"
        assert t.scope_spec["channel"] == "default"
        assert t.scope_spec["language"] == "pl"
        assert t.scope_spec["row_type"] == t.type


@pytest.mark.django_db
def test_import_csv_rejects_malformed_without_spawning(staging_dir):
    from django_enrichment.models import EnrichmentTask

    with pytest.raises(ValueError):
        task_service.import_csv(file=_csv("bad,header,here\nA,b,c\n"), module="pim", channel="default", language="pl")
    assert EnrichmentTask.objects.count() == 0


@pytest.mark.django_db
def test_spawn_csv_requires_file_ref(staging_dir):
    with pytest.raises(ValueError, match="scope_spec.file"):
        task_service.spawn(type="fix-attribute", scope_spec={"mode": "csv", "module": "pim"})


# ── resolve_targets (mode:csv): filter by work-type, lazy paging ───────────────────────────────


@pytest.mark.django_db
def test_resolve_targets_csv_filters_by_work_type_and_pages(staging_dir):
    # 150 translate rows + 3 fix-attribute rows — exercises the page boundary (CSV_PAGE_SIZE=100).
    rows = ["sku,field,type"]
    rows += [f"SKU-{i},description,translate" for i in range(150)]
    rows += [f"SKU-F{i},name,fix-attribute" for i in range(3)]
    tasks = task_service.import_csv(file=_csv("\n".join(rows) + "\n"), module="pim", channel="eu", language="en")
    by_type = {t.type: t for t in tasks}

    page1 = task_service.resolve_targets(by_type["translate"], page=1)
    page2 = task_service.resolve_targets(by_type["translate"], page=2)
    fix = task_service.resolve_targets(by_type["fix-attribute"], page=1)

    assert len(page1) == csv_import.CSV_PAGE_SIZE  # 100
    assert len(page2) == 50  # remainder — only this work-type's rows
    assert page1[0] == {"subject_ref": "SKU-0", "field": "description"}
    assert {t["subject_ref"] for t in fix} == {"SKU-F0", "SKU-F1", "SKU-F2"}  # other type filtered out


@pytest.mark.django_db
def test_resolve_targets_csv_past_end_is_empty(staging_dir):
    tasks = task_service.import_csv(file=_csv(_GOOD), module="pim", channel="default", language="pl")
    a_task = tasks[0]
    assert task_service.resolve_targets(a_task, page=99) == []
