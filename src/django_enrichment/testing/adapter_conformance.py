# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Adapter conformance kit — one shared suite every enrichment adapter must pass (test plan § 1, L2).

A module's test suite subclasses the classes below and provides the fixtures they name; the classes
have no `Test` prefix, so pytest collects only the subclasses. Example (PIM)::

    from django_enrichment.testing.adapter_conformance import (
        AdapterConformance, ConformanceCase, FieldCatalogConformance,
    )
    from django_pim.services import enrichment_adapter

    @pytest.fixture
    def adapter():
        return enrichment_adapter

    @pytest.fixture(params=["text", "select"])
    def conformance_case(request, product_factory) -> ConformanceCase:
        ...  # seed one product, return the target and two valid values

    @pytest.fixture
    def catalog_channel(channel_factory) -> str:
        return channel_factory().idx

    class TestPimConformance(AdapterConformance, FieldCatalogConformance):
        pass

The adapter receives a `ConformanceProposal` (the attributes adapters read from `ContentProposal`), so
the kit needs no enrichment tables. `DocumentCreateConformance` is for adapters that create documents
(`apply` returns `{"subject_ref"}`); it needs a `document_case` fixture.
"""

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

FIELD_TYPES = frozenset({"text", "html", "select", "multiselect"})
SELECT_TYPES = frozenset({"select", "multiselect"})
MAX_OPTIONS = 200


@dataclass
class ConformanceCase:
    """One target seeded by the module's fixture factory, with two different values valid for it."""

    subject_ref: str
    target_kind: str
    target_locator: dict
    value: dict
    other_value: dict


@dataclass
class ConformanceProposal:
    """Duck-typed stand-in for `ContentProposal` — what an adapter's `apply` / `revert` read."""

    subject_ref: str
    target_kind: str
    target_locator: dict
    proposed_value: dict
    current_snapshot: dict = field(default_factory=dict)
    applied_snapshot: dict = field(default_factory=dict)
    result_ref: str = ""
    pk: int | None = None
    reviewed_by: Any = None


def read(adapter: ModuleType, case: ConformanceCase) -> dict:
    return adapter.read_current(
        subject_ref=case.subject_ref, target_kind=case.target_kind, target_locator=case.target_locator
    )


def proposal(case: ConformanceCase, value: dict, snapshot: dict | None = None) -> ConformanceProposal:
    return ConformanceProposal(
        subject_ref=case.subject_ref,
        target_kind=case.target_kind,
        target_locator=case.target_locator,
        proposed_value=value,
        current_snapshot=snapshot or {},
    )


@pytest.mark.django_db
class AdapterConformance:
    """`read_current` / `apply` / `revert` / drift. Fixtures: `adapter`, `conformance_case`."""

    def test_exposes_the_contract(self, adapter):
        for name in ("resolve_targets", "find_gaps", "read_current", "apply", "revert"):
            assert callable(getattr(adapter, name, None)), name

    def test_read_current_returns_a_dict(self, adapter, conformance_case):
        assert isinstance(read(adapter, conformance_case), dict)

    def test_apply_writes_the_value(self, adapter, conformance_case):
        adapter.apply(proposal(conformance_case, conformance_case.value, read(adapter, conformance_case)))
        assert read(adapter, conformance_case) == conformance_case.value

    def test_revert_restores_the_snapshot(self, adapter, conformance_case):
        snapshot = read(adapter, conformance_case)
        applied = proposal(conformance_case, conformance_case.value, snapshot)
        adapter.apply(applied)
        adapter.revert(applied)
        assert read(adapter, conformance_case) == snapshot

    def test_drift_is_visible_in_read_current(self, adapter, conformance_case):
        """An edit made after our apply shows up in `read_current` — the bus's drift / undo-block check."""
        adapter.apply(proposal(conformance_case, conformance_case.value, read(adapter, conformance_case)))
        adapter.apply(proposal(conformance_case, conformance_case.other_value))
        assert read(adapter, conformance_case) == conformance_case.other_value != conformance_case.value


@pytest.mark.django_db
class FieldCatalogConformance:
    """`describe_fields` shape, `channel_languages`, `make_locator`. Fixtures: `adapter`, `catalog_channel`."""

    def test_describe_fields_shape(self, adapter, catalog_channel):
        fields = adapter.describe_fields(channel=catalog_channel, language="")
        assert isinstance(fields, list)
        assert len({item["key"] for item in fields}) == len(fields)
        for item in fields:
            assert_field_shape(item)

    def test_channel_languages_are_lowercase_iso2(self, adapter, catalog_channel):
        languages = adapter.channel_languages(channel=catalog_channel)
        assert languages, "a channel has at least its main language"
        assert len(set(languages)) == len(languages)
        assert all(isinstance(lang, str) and lang == lang.lower() and len(lang) == 2 for lang in languages)

    def test_make_locator_for_every_field(self, adapter, catalog_channel):
        language = adapter.channel_languages(channel=catalog_channel)[0]
        for item in adapter.describe_fields(channel=catalog_channel, language=language):
            lang = language if item["translatable"] and item["type"] not in SELECT_TYPES else None
            locator = adapter.make_locator(field_key=item["key"], channel=catalog_channel, language=lang)
            assert isinstance(locator, dict) and locator, item["key"]
            assert locator == adapter.make_locator(field_key=item["key"], channel=catalog_channel, language=lang)


def assert_field_shape(item: dict) -> None:
    """One catalog entry as contract A3 § 2 defines it."""
    assert isinstance(item["key"], str) and item["key"]
    assert isinstance(item["label"], str) and item["label"]
    assert item["type"] in FIELD_TYPES, item
    assert item["max_length"] is None or (isinstance(item["max_length"], int) and item["max_length"] > 0)
    assert isinstance(item["translatable"], bool)
    if item["type"] in SELECT_TYPES:
        options = item["options"]
        assert len(options) <= MAX_OPTIONS
        assert all(isinstance(option["value"], str) and isinstance(option["label"], str) for option in options)
        assert isinstance(item["options_truncated"], bool)


@pytest.mark.django_db
class DocumentCreateConformance:
    """`apply` of a document target returns `{"subject_ref"}`; `revert` undoes it. Fixtures: `adapter`, `document_case`."""

    def test_apply_returns_the_created_subject_ref(self, adapter, document_case):
        result = adapter.apply(proposal(document_case, document_case.value))
        assert isinstance(result, dict) and isinstance(result.get("subject_ref"), str) and result["subject_ref"]

    def test_revert_of_a_created_document_succeeds(self, adapter, document_case):
        created = proposal(document_case, document_case.value)
        created.result_ref = adapter.apply(created)["subject_ref"]
        adapter.revert(created)
