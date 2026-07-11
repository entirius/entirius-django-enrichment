# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Smoke tests for the `seed_demo_proposals` demo-data command."""

import pytest
from django.core.management import call_command

from django_enrichment.enums import ProposalStatus
from django_enrichment.models import ContentProposal


@pytest.fixture
def staging_dir(settings, tmp_path):
    settings.ENRICHMENT_STAGING_DIR = str(tmp_path)
    return tmp_path


@pytest.mark.django_db
def test_seed_populates_text_and_picture_proposals(fake_adapter, staging_dir):
    call_command("seed_demo_proposals", verbosity=0)

    pending = ContentProposal.objects.filter(status=ProposalStatus.PENDING.value)
    assert pending.filter(target_kind="text").count() >= 10
    pictures = pending.filter(target_kind="picture")
    assert pictures.count() >= 4
    assert all(p.staged_file for p in pictures)  # binaries staged in the bus's own store


@pytest.mark.django_db
def test_seed_is_idempotent(fake_adapter, staging_dir):
    call_command("seed_demo_proposals", verbosity=0)
    first = ContentProposal.objects.count()
    call_command("seed_demo_proposals", verbosity=0)
    assert ContentProposal.objects.count() == first  # external_ref dedupe → no duplicates
