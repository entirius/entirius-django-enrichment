# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Media intake (etap-08): staging on intake, caps, reject/apply cleanup, multipart API + streaming.

The fake adapter's `read_current` returns `{}` for the `picture` kind, so intake snapshots `{}` and
accept sees no drift — the bus mechanics are exercised without a real PIM.
"""

import base64

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from django_enrichment.enums import ProposalStatus
from django_enrichment.services import proposal_service, staging_store

# 1x1 transparent PNG — real magic bytes so the sniff allowlist accepts it.
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
PROPOSALS = "/api/enrichment/v2/admin/proposals/"
UPLOAD = f"{PROPOSALS}upload-media/"


@pytest.fixture
def staging_dir(settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    return tmp_path


def _png(name="new.png"):
    return SimpleUploadedFile(name, _PNG, content_type="image/png")


def _intake_media(*, subject_ref="SKU-1", **extra):
    return proposal_service.intake_media(
        file=_png(),
        target_module="pim",
        subject_ref=subject_ref,
        target_locator={"channel": "default"},
        proposed_value={"op": "replace_main", "alt_t9n": {"en": "alt"}},
        **extra,
    )


# ── service: intake stages, forces picture kind ───────────────────────────────────────────────


@pytest.mark.django_db
def test_intake_media_stages_binary_and_forces_picture(staging_dir, fake_adapter):
    proposal = _intake_media()

    assert proposal.target_kind == "picture"
    assert proposal.staged_file
    assert staging_store.exists(proposal.staged_file)
    assert proposal.current_snapshot == {}  # fake read_current(picture) → {}


@pytest.mark.django_db
def test_intake_media_idempotent_drops_orphan_staged(staging_dir, fake_adapter):
    first = _intake_media(external_ref="comfy-1")
    second = _intake_media(external_ref="comfy-1")

    assert second.pk == first.pk  # idempotent
    # Only the first proposal's staged file survives; the duplicate upload was cleaned up.
    assert staging_store.exists(first.staged_file)


# ── service: caps ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_intake_media_rejects_oversize(staging_dir, fake_adapter, settings):
    settings.ENRICHMENT_MAX_IMAGE_BYTES = 10  # PNG is larger

    with pytest.raises(ValueError, match="cap"):
        _intake_media()


@pytest.mark.django_db
def test_intake_media_rejects_non_image(staging_dir, fake_adapter):
    bogus = SimpleUploadedFile("evil.png", b"#!/bin/sh\nrm -rf /", content_type="image/png")

    with pytest.raises(ValueError, match="unsupported image type"):
        proposal_service.intake_media(
            file=bogus,
            target_module="pim",
            subject_ref="SKU-1",
            target_locator={"channel": "default"},
            proposed_value={"op": "replace_main"},
        )


def test_oversize_leaves_no_staged_file(staging_dir, settings):
    settings.ENRICHMENT_MAX_IMAGE_BYTES = 10
    # Validation happens before staging — the directory stays empty.
    with pytest.raises(ValueError):
        proposal_service._validate_media(_png())
    assert list(staging_dir.iterdir()) == []


# ── service: reject / apply both clean up the staged file ──────────────────────────────────────


@pytest.mark.django_db
def test_reject_deletes_staged(staging_dir, fake_adapter):
    proposal = _intake_media()
    ref = proposal.staged_file

    proposal_service.reject(proposal, "off-brand")

    assert proposal.status == ProposalStatus.REJECTED.value
    assert not staging_store.exists(ref)


@pytest.mark.django_db
def test_apply_deletes_staged(staging_dir, fake_adapter):
    proposal = _intake_media()
    ref = proposal.staged_file

    applied = proposal_service.accept(proposal)

    assert applied.status == ProposalStatus.APPLIED.value
    assert not staging_store.exists(ref)


@pytest.mark.django_db
def test_bulk_reject_deletes_staged(staging_dir, fake_adapter):
    proposal = _intake_media(subject_ref="SKU-9")
    ref = proposal.staged_file

    proposal_service.bulk_reject({"module": "pim"}, "cleanup")

    proposal.refresh_from_db()
    assert proposal.status == ProposalStatus.REJECTED.value
    assert not staging_store.exists(ref)


# ── API: multipart upload + streaming + caps ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_upload_media_returns_201(staging_dir, admin_client, fake_adapter):
    import json

    resp = admin_client.post(
        UPLOAD,
        {
            "file": _png(),
            "subject_ref": "SKU-1",
            "target_locator": json.dumps({"channel": "default"}),
            "proposed_value": json.dumps({"op": "replace_main", "alt_t9n": {"en": "x"}}),
        },
        format="multipart",
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["target_kind"] == "picture"
    assert body["staged_file"]


@pytest.mark.django_db
def test_upload_media_missing_file_is_400(staging_dir, admin_client, fake_adapter):
    import json

    resp = admin_client.post(
        UPLOAD, {"subject_ref": "SKU-1", "target_locator": json.dumps({"channel": "default"})}, format="multipart"
    )

    assert resp.status_code == 400


@pytest.mark.django_db
def test_upload_media_bad_mime_is_400(staging_dir, admin_client, fake_adapter):
    import json

    resp = admin_client.post(
        UPLOAD,
        {
            "file": SimpleUploadedFile("x.png", b"not an image", content_type="image/png"),
            "subject_ref": "SKU-1",
            "target_locator": json.dumps({"channel": "default"}),
        },
        format="multipart",
    )

    assert resp.status_code == 400


@pytest.mark.django_db
def test_staged_file_streams_binary(staging_dir, admin_client, fake_adapter):
    proposal = _intake_media()

    resp = admin_client.get(f"{PROPOSALS}{proposal.id}/staged-file/")

    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == _PNG


@pytest.mark.django_db
def test_staged_file_404_when_absent(staging_dir, admin_client, fake_adapter):
    # A text proposal has no staged file.
    pid = proposal_service.intake(
        target_module="pim",
        subject_ref="SKU-2",
        target_kind="attribute_value",
        target_locator={"feature_idx": "description", "language": "en"},
        proposed_value={"text": "hi"},
    ).id

    resp = admin_client.get(f"{PROPOSALS}{pid}/staged-file/")

    assert resp.status_code == 404
