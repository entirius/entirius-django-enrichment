# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""In-memory enrichment adapter for testing the bus without a real source module.

A module-as-adapter (the same shape the PIM adapter takes in etap-04): module-level functions
matching `adapters.base.EnrichmentAdapter`, plus test helpers (`set_current`, `reset`). Wired via
`override_settings(ENRICHMENT_ADAPTERS={"pim": "tests.fake_adapter"})` — see `conftest.fake_adapter`.

`_STORE` is the fake module's "current value" keyed by target, so `read_current` / `apply` / `revert`
round-trip without touching any database table outside enrichment's own.
"""

from typing import Any

from django_enrichment.models import compute_locator_hash

_STORE: dict[tuple[str, str, str], dict] = {}

# Records every proposal pk passed to `release_undo_anchor` (etap-10) so cleanup tests can assert
# the bus invokes the GC hook for the right (picture) proposals.
_RELEASED: list[int] = []


def _key(subject_ref: str, target_kind: str, target_locator: dict) -> tuple[str, str, str]:
    return subject_ref, target_kind, compute_locator_hash(target_locator)


# ── adapter contract ──────────────────────────────────────────────────────────────────────


def resolve_targets(scope_spec: dict, page: int = 1) -> list[Any]:  # noqa: ARG001 — page unused by fake
    """filter / csv / list resolution — the fake just echoes the explicit refs."""
    return list(scope_spec.get("refs", []))


def find_gaps(check: str, params: dict, scope: dict) -> list[Any]:  # noqa: ARG001 — params unused by fake
    """gap resolution — return seeded candidates, or a single deterministic marker."""
    return list(scope.get("candidates", [f"gap:{check}"]))


def read_current(*, subject_ref: str, target_kind: str, target_locator: dict) -> dict:
    return dict(_STORE.get(_key(subject_ref, target_kind, target_locator), {}))


def apply(proposal: Any) -> None:
    _STORE[_key(proposal.subject_ref, proposal.target_kind, proposal.target_locator)] = dict(proposal.proposed_value)


def revert(proposal: Any) -> None:
    _STORE[_key(proposal.subject_ref, proposal.target_kind, proposal.target_locator)] = dict(proposal.current_snapshot)


def release_undo_anchor(proposal: Any) -> int:
    """Etap-10 GC hook — the fake has no external blob store, so it just records the call and
    reports zero reclaimed. Lets cleanup tests assert which proposals the bus offered for GC."""
    _RELEASED.append(proposal.pk)
    return 0


# ── test helpers ──────────────────────────────────────────────────────────────────────────


def set_current(subject_ref: str, target_kind: str, target_locator: dict, value: dict) -> None:
    _STORE[_key(subject_ref, target_kind, target_locator)] = dict(value)


def released() -> list[int]:
    """Proposal pks passed to `release_undo_anchor` since the last reset."""
    return list(_RELEASED)


def reset() -> None:
    _STORE.clear()
    _RELEASED.clear()
