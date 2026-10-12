---
title: Testing
description: How the enrichment test suite runs and what the in-memory fake adapter is for.
---

```bash
make test                              # pytest (sqlite in-memory, --no-migrations)
make check                             # ruff check + format check
```

`addopts = --no-migrations` (pytest-django builds the schema straight from the models). Suites include
`test_smoke` (DB-less), `test_models`, `test_registry`, `test_task_service`, `test_proposal_service`,
`test_cleanup` (retention prune). The in-memory `tests/fake_adapter.py` (a module-as-adapter) lets the
whole bus flow be tested without `django-pim`; `conftest.fake_adapter` registers it via
`override_settings`. Its `release_undo_anchor` is a spy (`fake_adapter.released()`) so cleanup tests
can assert the bus invokes the GC hook for the right proposals.
