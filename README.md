# entirius-django-enrichment

A Django module for reviewed content enrichment in Entirius, the open-source modular ecommerce platform.
It runs an accept-before-write loop for product content (descriptions, attributes, SEO, media): typed tasks are
spawned, an external worker returns proposals, an operator reviews them, and only accepted proposals are written
back to the source module through an adapter.

[![PyPI](https://img.shields.io/pypi/v/entirius-django-enrichment.svg)](https://pypi.org/project/entirius-django-enrichment/)
[![Python](https://img.shields.io/pypi/pyversions/entirius-django-enrichment.svg)](https://pypi.org/project/entirius-django-enrichment/)
[![License: MPL-2.0](https://img.shields.io/badge/license-MPL--2.0-blue.svg)](https://github.com/entirius/entirius-django-enrichment/blob/develop/LICENSE)

## What it does

- Spawns enrichment tasks over a scope of products: a list, a filter, a CSV file or a quality gap.
- Takes proposals from an external worker, one per field, with idempotent intake and staged media files.
- Offers a review queue with single and bulk accept and reject; a value that changed since the proposal was
  made is never overwritten without a second confirmation.
- Writes accepted proposals through a per-module adapter and supports a single-level undo.
- Runs spawn rules on a schedule and prunes finished work after a retention window.
- Exposes an admin API (JWT, Pydantic schemas, OpenAPI) for the CMS and the worker.

## Installation

```shell
uv add entirius-django-enrichment
pip install entirius-django-enrichment
```

Optional extras: `entirius-django-enrichment[demo]` (Pillow, for the `seed_demo_proposals` command).

```python
INSTALLED_APPS = [
    ...
    "django_utils",
    "django_enrichment",
]

# urls.py
urlpatterns = [
    path("", include("django_enrichment.urls")),
]

# adapters of the source modules in use
ENRICHMENT_ADAPTERS = {"pim": "django_pim.services.enrichment_adapter"}
```

`ENRICHMENT_ADAPTERS` is the one setting a working installation needs; the other `ENRICHMENT_*` settings
are listed with their defaults in https://github.com/entirius/entirius-django-enrichment/blob/develop/AGENTS.md (section Settings).

## Quick start

```python
from django_enrichment.services import task_service

task = task_service.spawn(
    type="translate",
    scope_spec={"mode": "list", "module": "pim", "refs": ["SKU-1", "SKU-2"]},
    params={"language": "de"},
)
```

The worker then pulls the task and posts proposals to `api/enrichment/v2/admin/proposals/`.

## Requirements

- Python 3.11 or newer, Django 5.0 or newer.
- A Celery worker consuming the `enrichment` queue (bulk apply, undo, retention, scheduled spawn rules).
- Entirius packages pulled in: `entirius-django-utils`. Adapters live in the source modules; the catalog
  adapter ships with `entirius-django-pim` and is loaded lazily, so this module never imports it.

## Documentation

- Overview: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/index.md
- Data model: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/data-model.md
- Admin API: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/api.md
- Service contracts: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/services.md
- Adapter guide: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/adapter-guide.md
- Media proposals: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/media.md
- Scale, undo and retention: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/scale-and-undo.md
- Spawn rules: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/spawn-rules.md
- Spawn surfaces: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/spawn-surfaces.md
- Management commands: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/commands.md
- Testing: https://github.com/entirius/entirius-django-enrichment/blob/develop/docs/testing.md
- Entirius documentation: https://docs.entirius.com/
- Changelog: https://github.com/entirius/entirius-django-enrichment/blob/develop/CHANGELOG.md

## Part of Entirius

This module runs inside the Entirius backend service (Volkanos) and improves the content other modules own.

- Backend service: https://github.com/entirius/entirius-service-volkanos
- Docker harness: https://github.com/entirius/entirius-zeno
- Sibling modules: https://github.com/entirius/entirius-django-pim, https://github.com/entirius/entirius-django-atlas,
  https://github.com/entirius/entirius-django-lookup

## Development

```shell
make install     # sync dependencies (uv, incl. extras)
make check       # lint + format check (ruff)
make test        # test suite (pytest + pytest-django, sqlite in memory)
```

Development and agent instructions: https://github.com/entirius/entirius-django-enrichment/blob/develop/AGENTS.md

## License

Mozilla Public License 2.0 (MPL-2.0) — see https://github.com/entirius/entirius-django-enrichment/blob/develop/LICENSE.
