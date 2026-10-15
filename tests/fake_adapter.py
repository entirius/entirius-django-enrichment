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

from django.core.exceptions import ObjectDoesNotExist

from django_enrichment.models import compute_locator_hash

_STORE: dict[tuple[str, str, str], dict] = {}

# Records every proposal pk passed to `release_undo_anchor` (etap-10) so cleanup tests can assert
# the bus invokes the GC hook for the right (picture) proposals.
_RELEASED: list[int] = []

# Context served by `build_context`; extra keys from `set_context` are merged in (injection tests).
_CONTEXT: dict = {}

# Field catalog served by the optional extras (`describe_fields`, `make_locator`); empty by default so
# tests that expect "no catalog" keep that behaviour — seed it with `set_catalog`.
_CATALOG: list[dict] = []
_LANGUAGES = ["pl", "en", "de"]

# Subjects `subject_labels` knows in channel "default" (`{ref: label}`; any other channel is unknown) — `set_subjects`.
_SUBJECTS: dict[str, str] = {}

# Documents created by `apply` of a `target_kind="document"` proposal, keyed by the returned subject ref.
DOCUMENT_KIND = "document"
_DOCUMENTS: dict[str, dict] = {}

# Records every proposal pk passed to the optional `on_reject` hook, so the reject tests can assert
# the bus notifies the source module (atlas keeps a durable "not the same product" this way).
_REJECTED: list[int] = []


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


def apply(proposal: Any) -> dict | None:
    if proposal.target_kind == DOCUMENT_KIND:
        return _create_document(proposal)
    _STORE[_key(proposal.subject_ref, proposal.target_kind, proposal.target_locator)] = dict(proposal.proposed_value)
    return None


def revert(proposal: Any) -> None:
    if proposal.target_kind == DOCUMENT_KIND:
        _DOCUMENTS.pop(proposal.result_ref, None)
        return
    _STORE[_key(proposal.subject_ref, proposal.target_kind, proposal.target_locator)] = dict(proposal.current_snapshot)


def _create_document(proposal: Any) -> dict:
    """`target_kind="document"` creates a new subject and returns its ref (contract A4 § 6)."""
    ref = f"doc-{len(_DOCUMENTS) + 1}"
    _DOCUMENTS[ref] = dict(proposal.proposed_value)
    return {"subject_ref": ref}


def on_reject(proposal: Any) -> None:
    """Optional reject hook — the fake only records that the bus called it."""
    _REJECTED.append(proposal.pk)


def release_undo_anchor(proposal: Any) -> int:
    """Etap-10 GC hook — the fake has no external blob store, so it just records the call and
    reports zero reclaimed. Lets cleanup tests assert which proposals the bus offered for GC."""
    _RELEASED.append(proposal.pk)
    return 0


# ── optional extras (contract A3 § 2) ──────────────────────────────────────────────────────


def describe_fields(*, channel: str, language: str = "") -> list[dict]:  # noqa: ARG001 — one catalog for all
    return [dict(field) for field in _CATALOG]


def channel_languages(*, channel: str) -> list[str]:  # noqa: ARG001 — one language set for all channels
    return list(_LANGUAGES)


def make_locator(*, field_key: str, channel: str, language: str | None) -> dict:
    return {"channel": channel, "language": language, "feature_idx": field_key}


def field_key(*, locator: dict) -> str | None:
    return locator.get("feature_idx")


def subject_labels(*, refs: list[str], channel: str) -> dict[str, str]:
    if channel != "default":
        raise ObjectDoesNotExist(channel)
    return {ref: _SUBJECTS[ref] for ref in refs if ref in _SUBJECTS}


def build_context(*, subject_ref: str, channel: str, language: str) -> dict:
    return {"sku": subject_ref, "channel": channel, "language": language, **_CONTEXT}


# ── test helpers ──────────────────────────────────────────────────────────────────────────


def set_current(subject_ref: str, target_kind: str, target_locator: dict, value: dict) -> None:
    _STORE[_key(subject_ref, target_kind, target_locator)] = dict(value)


def set_catalog(fields: list[dict]) -> None:
    _CATALOG[:] = [dict(field) for field in fields]


def set_subjects(labels: dict[str, str]) -> None:
    _SUBJECTS.clear()
    _SUBJECTS.update(labels)


def set_context(extra: dict) -> None:
    _CONTEXT.clear()
    _CONTEXT.update(extra)


def documents() -> dict[str, dict]:
    return dict(_DOCUMENTS)


def released() -> list[int]:
    """Proposal pks passed to `release_undo_anchor` since the last reset."""
    return list(_RELEASED)


def rejected() -> list[int]:
    """Proposal pks passed to `on_reject` since the last reset."""
    return list(_REJECTED)


def reset() -> None:
    _STORE.clear()
    _CATALOG.clear()
    _CONTEXT.clear()
    _SUBJECTS.clear()
    _DOCUMENTS.clear()
    _RELEASED.clear()
    _REJECTED.clear()
