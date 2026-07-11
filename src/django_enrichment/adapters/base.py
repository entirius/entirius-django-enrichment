# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Adapter contract for the enrichment bus (decision #11, D1).

An *adapter* is the per-module READ/WRITE boundary the bus routes through. The bus never
imports a source module (PIM, ContentDB, ...) directly — it loads the adapter lazily from
`settings.ENRICHMENT_ADAPTERS` keyed by `target_module` (see `registry.get_adapter`).

The contract is duck-typed: an adapter is any Python *module* exposing the five callables
below. This `Protocol` is documentary — it is not enforced at runtime — so a source module
can register `"django_pim.services.enrichment_adapter"` (a module path) without importing or
subclassing anything from `django-enrichment`. That keeps the dependency one-directional:
the bus depends on the contract, never on the concrete module.

Golden rules (analiza § Wielomodułowość, trzy zasady):
- `proposed_value` / `current_snapshot` are OPAQUE to the bus — only the adapter reads them.
- `read_current` / drift / `revert` semantics belong to the adapter (PIM materialises
  inheritance write-time; ContentDB may version its own way). The bus stays dumb.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EnrichmentAdapter(Protocol):
    """The five callables a source-module adapter must expose. Module-level functions.

    Only `resolve_targets`, `find_gaps`, `read_current` and `apply` are exercised by the
    services in this stage (etap-03); `revert` is part of the contract but is first used by
    the undo flow (etap-09). The PIM bodies land in `django-pim` in etap-04 — until then the
    test fake (`tests/fake_adapter.py`) implements the contract in memory.
    """

    def resolve_targets(self, scope_spec: dict, page: int = 1) -> list[Any]:
        """Resolve a `filter` / `csv` / `list` scope into concrete targets (READ, lazy/paged).

        Must NOT materialise the whole catalogue — the bus passes `page` so the adapter can
        stream. `filter` is a simplified `gap` (no named check); `gap` goes to `find_gaps`.
        """
        ...

    def find_gaps(self, check: str, params: dict, scope: dict) -> list[Any]:
        """Run a named quality check over the module's data and return candidates (READ).

        `scope_spec.mode == "gap"` routes here. The real PIM catalogue of checks lands with
        the PIM adapter (etap-04 / pim-quality-score). `filter` ⇄ `gap` are the same family:
        find targets via a module-side query.
        """
        ...

    def read_current(self, *, subject_ref: str, target_kind: str, target_locator: dict) -> dict:
        """Authoritative current value of a target → `current_snapshot` (READ).

        Source of truth for diff / drift / undo. The bus calls this at intake (never trusting
        a worker-supplied "before") and again before apply (drift check, D2).
        """
        ...

    def apply(self, proposal: Any) -> None:
        """Write `proposal.proposed_value` into the source module (WRITE).

        The ONLY place enrichment writes back to a module. Body is module-specific and opaque
        to the bus. `apply_service` calls this after the drift check passes.

        Media (`target_kind == "picture"`, etap-08): the binary is NOT in `proposed_value`. The
        adapter reads the staged bytes via `proposal.open_staged_file()` (a Django `File`) — never
        importing the bus's `staging_store` (the proposal carries its own opener, decision D1).
        Convention: `proposed_value = {"op": "replace_main", "alt_t9n": {...}}`;
        `current_snapshot = {"sha1", "role", "position", "alt_t9n", "url"}` of the displaced main
        (empty `{}` when none). The bus drops the staged file after apply; the displaced source
        object (e.g. the old PIM `Picture`) is the undo anchor for `revert`.
        """
        ...

    def revert(self, proposal: Any) -> None:
        """Restore the target to `proposal.current_snapshot` (WRITE, undo — etap-09)."""
        ...

    def release_undo_anchor(self, proposal: Any) -> int:
        """GC the module's external undo anchor for a proposal being pruned (WRITE, OPTIONAL — etap-10).

        Optional: the cleanup beat calls it via `getattr`, so an adapter whose targets keep no
        external anchor (text — the snapshot lives in the proposal row) need not implement it. The
        PIM picture adapter implements it: once retention expires and the bus deletes an applied /
        reverted media proposal, the displaced/orphaned `Picture` it anchored is GC-ed — but ONLY
        when nothing else references it (SHA1 dedup is shared; never delete someone else's image).

        Must be best-effort and reference-guarded: cleanup logs and continues on failure, and a
        re-run must not double-delete. Returns the count of external blobs reclaimed (0 = nothing).
        """
        ...
