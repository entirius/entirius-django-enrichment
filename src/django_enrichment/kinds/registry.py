# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind registry. The bus registers `translate` and `field` in `EnrichmentConfig.ready()`; other
modules register their own kinds the same way from their own `ready()`."""

from django_enrichment.kinds.base import Kind

_KINDS: dict[str, type[Kind]] = {}


def register_kind(name: str, kind_cls: type[Kind]) -> None:
    _KINDS[name] = kind_cls


def get_kind(name: str) -> type[Kind]:
    """The registered kind class. Raises `ValueError` for an unknown name."""
    if name not in _KINDS:
        raise ValueError(f"unknown worker kind {name!r} (known: {sorted(_KINDS)})")
    return _KINDS[name]
