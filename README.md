# django-enrichment

Horizontal content-quality bus for the Volkanos platform — an accept-before-write enrichment loop
for product content (descriptions, attributes, SEO, media). The CMS spawns typed enrichment tasks,
an external worker (n8n) generates proposals, an operator reviews them, and accepted proposals are
written back to the source module (PIM first) through a per-module adapter.

## Installation

```shell
pip install entirius-django-enrichment
```

Add the app to your project:

```python
INSTALLED_APPS = [
    ...
    "django_enrichment",
]
```

Register adapters for the source modules you use:

```python
ENRICHMENT_ADAPTERS = {"pim": "django_pim.services.enrichment_adapter"}
```

## Development

```shell
make install     # sync dependencies (uv)
make check       # lint + format check (ruff)
make test        # test suite (pytest + pytest-django)
```

Architecture, API contract and agent instructions: [AGENTS.md](AGENTS.md), [docs/](docs/).

## License

Mozilla Public License 2.0 — see [LICENSE](LICENSE).
