# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Staging-store backend (etap-08): save / open / delete / exists + path-traversal guard."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from django_enrichment.services import staging_store


@pytest.fixture
def staging_dir(settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    return tmp_path


def _upload(content=b"binary-bytes", name="x.png"):
    return SimpleUploadedFile(name, content, content_type="application/octet-stream")


def test_save_returns_ref_and_persists(staging_dir):
    ref = staging_store.save(_upload(b"hello"))

    assert ref  # opaque non-empty ref
    assert staging_store.exists(ref)
    assert (staging_dir / ref).read_bytes() == b"hello"


def test_save_preserves_extension(staging_dir):
    ref = staging_store.save(_upload(name="photo.webp"))

    assert ref.endswith(".webp")


def test_open_file_round_trips_bytes(staging_dir):
    ref = staging_store.save(_upload(b"round-trip"))

    with staging_store.open_file(ref) as fh:
        assert fh.read() == b"round-trip"


def test_delete_is_idempotent(staging_dir):
    ref = staging_store.save(_upload())

    staging_store.delete(ref)
    assert not staging_store.exists(ref)
    staging_store.delete(ref)  # second delete must not raise
    staging_store.delete("")  # empty ref is a no-op


def test_exists_false_for_blank_or_missing(staging_dir):
    assert staging_store.exists("") is False
    assert staging_store.exists("nope.png") is False


@pytest.mark.parametrize("ref", ["../escape.png", "sub/dir.png", "a\\b.png", "..", ""])
def test_traversal_refs_are_rejected(staging_dir, ref):
    with pytest.raises(ValueError, match="unsafe staging ref|no staging"):
        staging_store.open_file(ref)
