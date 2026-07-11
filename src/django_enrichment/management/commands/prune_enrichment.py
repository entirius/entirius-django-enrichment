# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Manually run the enrichment retention prune (the same work the daily beat does).

Default window is `ENRICHMENT_RETENTION_DAYS`; `--days N` overrides it for a one-off cleanup or a
test (`--days 0` prunes everything currently qualifying). Delegates to `cleanup_service.prune` —
no business logic here (django-suppliers `prune_supplier_change_logs` style).
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Prune terminal enrichment proposals + done tasks older than the retention window, GC anchors."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Override ENRICHMENT_RETENTION_DAYS for this run (0 prunes everything qualifying).",
        )

    def handle(self, *args, **options) -> None:
        from django_enrichment.services import cleanup_service

        try:
            result = cleanup_service.prune(days=options.get("days"))
        except Exception as exc:  # noqa: BLE001 — surface any failure as a clean CLI error
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Prune complete: {result}"))
