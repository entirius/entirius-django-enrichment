# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Lazy adapter registry (DIP) — the bus's only indirection to source modules.

`settings.ENRICHMENT_ADAPTERS` maps `target_module` → dotted module path, e.g.::

    ENRICHMENT_ADAPTERS = {"pim": "django_pim.services.enrichment_adapter"}

`get_adapter` reads `django.conf.settings` **on every call** (finding etap-01): the PIM
adapter self-registers later, so a constant snapshotted at import time would diverge from
the live settings. The import itself is cached by dotted path — re-pointing the registry in
tests (`override_settings`) yields a fresh adapter without a stale cache hit.
"""

import importlib
from typing import Any

from django.conf import settings

# Cache keyed by dotted path (NOT by target_module): swapping the registry to a different
# path in tests must miss the cache, but re-importing the same path must not.
_ADAPTER_CACHE: dict[str, Any] = {}


def _registry() -> dict[str, str]:
    return getattr(settings, "ENRICHMENT_ADAPTERS", {})


def get_adapter(target_module: str) -> Any:
    """Return the registered adapter module for `target_module` (lazy import, cached).

    Raises `ValueError` with a clear message when no adapter is registered — a client without
    a given source module simply has no entry, which is a configuration fact, not a crash.
    """
    registry = _registry()
    dotted_path = registry.get(target_module)
    if not dotted_path:
        raise ValueError(
            f"no enrichment adapter for target_module={target_module!r} "
            f"(known: {sorted(registry)}); register it in settings.ENRICHMENT_ADAPTERS"
        )
    if dotted_path not in _ADAPTER_CACHE:
        _ADAPTER_CACHE[dotted_path] = importlib.import_module(dotted_path)
    return _ADAPTER_CACHE[dotted_path]


def get_adapters() -> dict[str, Any]:
    """Resolve every registered adapter (keyed by target_module). Mostly for introspection."""
    return {module: get_adapter(module) for module in _registry()}


def clear_cache() -> None:
    """Drop the import cache. Test hook — production never re-points a live path."""
    _ADAPTER_CACHE.clear()
