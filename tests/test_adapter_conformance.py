# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The adapter conformance kit, proven on the in-memory fake adapter (text, select, document)."""

import pytest

from django_enrichment.testing.adapter_conformance import (
    AdapterConformance,
    ConformanceCase,
    DocumentCreateConformance,
    FieldCatalogConformance,
    assert_field_shape,
)

CATALOG = [
    {"key": "meta_title", "label": "Meta title", "type": "text", "max_length": 60, "translatable": True},
    {
        "key": "color",
        "label": "Color",
        "type": "select",
        "max_length": None,
        "translatable": False,
        "options": [{"value": "red", "label": "Red"}, {"value": "blue", "label": "Blue"}],
        "options_truncated": False,
    },
]
CASES = {
    "text": ConformanceCase(
        subject_ref="SKU-1",
        target_kind="attribute_value",
        target_locator={"channel": "default", "language": "pl", "feature_idx": "meta_title"},
        value={"text": "New title"},
        other_value={"text": "Edited elsewhere"},
    ),
    "select": ConformanceCase(
        subject_ref="SKU-1",
        target_kind="attribute_value",
        target_locator={"channel": "default", "language": None, "feature_idx": "color"},
        value={"option": "red"},
        other_value={"option": "blue"},
    ),
}


@pytest.fixture
def adapter(fake_adapter):
    fake_adapter.set_catalog(CATALOG)
    fake_adapter.set_current("SKU-1", "attribute_value", CASES["text"].target_locator, {"text": "Old title"})
    return fake_adapter


@pytest.fixture(params=sorted(CASES))
def conformance_case(request) -> ConformanceCase:
    return CASES[request.param]


@pytest.fixture
def catalog_channel() -> str:
    return "default"


@pytest.fixture
def document_case() -> ConformanceCase:
    value = {"language": "en", "title": "Hello", "slug": "hello", "body_markdown": "# Hello", "meta": {}, "faq": []}
    return ConformanceCase("new", "document", {"channel": "default"}, value, value)


class TestFakeAdapterConformance(AdapterConformance, FieldCatalogConformance, DocumentCreateConformance):
    pass


def test_shape_check_rejects_an_unknown_type():
    with pytest.raises(AssertionError):
        assert_field_shape({**CATALOG[0], "type": "rich"})


def test_shape_check_requires_the_truncated_flag_on_selects():
    with pytest.raises(KeyError):
        assert_field_shape({key: value for key, value in CATALOG[1].items() if key != "options_truncated"})
