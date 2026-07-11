# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""OpenAPI schema contract (etap-05).

Pins the admin API surface: every enrichment endpoint is present with the right methods, an
operationId, and a tag. (Body-component resolution + a warning-free generation are verified in the
host service via `manage.py spectacular` — the platform's Pydantic↔spectacular bridge lives there.)
"""

import pytest

_EXPECTED = {
    "/api/enrichment/v2/admin/tasks/": {"get", "post"},
    "/api/enrichment/v2/admin/tasks/{id}/": {"get", "patch"},
    "/api/enrichment/v2/admin/tasks/{id}/targets/": {"get"},
    "/api/enrichment/v2/admin/proposals/": {"get", "post"},
    "/api/enrichment/v2/admin/proposals/{id}/": {"get"},
    "/api/enrichment/v2/admin/proposals/{id}/accept/": {"post"},
    "/api/enrichment/v2/admin/proposals/{id}/reject/": {"post"},
    "/api/enrichment/v2/admin/proposals/bulk-accept/": {"post"},
    "/api/enrichment/v2/admin/proposals/bulk-reject/": {"post"},
    "/api/enrichment/v2/admin/proposals/bulk-undo/": {"post"},
}


def _schema():
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator().get_schema(request=None, public=True)


@pytest.mark.django_db
def test_schema_exposes_every_enrichment_endpoint():
    paths = _schema()["paths"]
    for path, methods in _EXPECTED.items():
        assert path in paths, f"missing path {path}"
        assert methods <= set(paths[path]), f"{path}: have {set(paths[path])}, want {methods}"


@pytest.mark.django_db
def test_every_enrichment_operation_has_operation_id_and_tag():
    paths = _schema()["paths"]
    for path, ops in paths.items():
        if "enrichment" not in path:
            continue
        for method, op in ops.items():
            assert op.get("operationId"), f"{method.upper()} {path} has no operationId"
            assert op.get("tags"), f"{method.upper()} {path} has no tag"
