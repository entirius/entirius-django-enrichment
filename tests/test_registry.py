# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest
from django.test import override_settings

from django_enrichment.adapters import clear_cache, get_adapter, get_adapters


def setup_function() -> None:
    clear_cache()


def test_get_adapter_missing_raises():
    with override_settings(ENRICHMENT_ADAPTERS={}), pytest.raises(ValueError, match="no enrichment adapter"):
        get_adapter("pim")


def test_get_adapter_lazy_import_and_cache():
    with override_settings(ENRICHMENT_ADAPTERS={"pim": "tests.fake_adapter"}):
        first = get_adapter("pim")
        second = get_adapter("pim")

    from tests import fake_adapter

    assert first is fake_adapter
    assert second is first  # cached by dotted path, same object


def test_get_adapter_reads_settings_per_call():
    # Snapshotting at import time would miss a later registry change (finding etap-01).
    with override_settings(ENRICHMENT_ADAPTERS={"pim": "tests.fake_adapter"}):
        assert get_adapter("pim") is not None
    with override_settings(ENRICHMENT_ADAPTERS={}), pytest.raises(ValueError, match="no enrichment adapter"):
        get_adapter("pim")


def test_get_adapters_maps_every_entry():
    with override_settings(ENRICHMENT_ADAPTERS={"pim": "tests.fake_adapter"}):
        adapters = get_adapters()

    assert set(adapters) == {"pim"}
