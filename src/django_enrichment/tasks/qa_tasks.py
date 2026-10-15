# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery entry of the automatic checklist check. Thin — the logic lives in `qa_auto_check`."""

from celery import shared_task


@shared_task(name="enrichment.qa_check_task")
def qa_check_task(proposal_ids: list[int]) -> None:
    from django_enrichment.services import qa_auto_check

    qa_auto_check.check_all(proposal_ids)
