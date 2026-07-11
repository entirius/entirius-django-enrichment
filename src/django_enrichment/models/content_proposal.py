# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import hashlib
import json

from django.db import models
from django_utils.models.base_model import BaseModel

from django_enrichment.enums import ProposalStatus


def compute_locator_hash(target_locator: dict | None) -> str:
    """Deterministic dedup key for the superseded lookup.

    Canonical JSON (sorted keys, no whitespace) so the hash is independent of key order.
    Non-cryptographic — it only materialises `target_locator` into an indexable column.
    """
    canonical = json.dumps(target_locator or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()  # noqa: S324 — non-crypto dedup key


class ContentProposal(BaseModel):
    """Row-per-field enrichment result with a generic, module-agnostic target (decision #11).

    The bus owns the target/value/metadata shape; it never interprets `proposed_value` or
    `current_snapshot` (opaque — only the target module's adapter writer reads them). PIM
    specifics (feature_idx, channel, language) live in `target_locator`, not as columns.
    State transitions / superseding / drift detection live in services (etap-03/04).
    """

    task = models.ForeignKey(
        "django_enrichment.EnrichmentTask", on_delete=models.SET_NULL, null=True, blank=True, related_name="proposals"
    )

    # Generic target (module-agnostic from day 0).
    # target_module/locator_hash are NOT standalone-indexed: target_module is the leading
    # key of enr_prop_target_locator_idx (prefix scan covers it) and locator_hash is only
    # ever queried as part of that full composite (superseded lookup) — separate indexes
    # would just be write amplification on a ~1M-row table.
    target_module = models.CharField(max_length=32)
    target_type = models.CharField(max_length=64, blank=True, default="")
    subject_ref = models.CharField(max_length=255, db_index=True)
    subject_label = models.CharField(max_length=512, blank=True, default="")
    subject_url = models.CharField(max_length=2048, blank=True, default="")
    target_kind = models.CharField(max_length=64)
    target_locator = models.JSONField(default=dict, blank=True)
    locator_hash = models.CharField(max_length=40, blank=True, default="", editable=False)

    # Generic value (opaque to the bus)
    proposed_value = models.JSONField(default=dict, blank=True)
    staged_file = models.CharField(max_length=512, blank=True, default="")
    current_snapshot = models.JSONField(default=dict, blank=True)
    # Post-apply truth captured by `apply_service` straight after the adapter write (etap-09). It is the
    # drift anchor for single-level undo: revert is refused if the live value no longer matches this, so a
    # post-apply edit by someone else is never silently clobbered. Generic (works for text and picture)
    # because it stores the adapter's own `read_current` result, not a per-module shape.
    applied_snapshot = models.JSONField(default=dict, blank=True)

    # Metadata
    status = models.CharField(
        max_length=20, choices=ProposalStatus.choices, default=ProposalStatus.PENDING, db_index=True
    )
    source = models.CharField(max_length=128, blank=True, default="")
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    batch_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    external_ref = models.CharField(max_length=255, blank=True, default="", db_index=True)
    reviewed_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["status", "subject_ref"], name="enr_prop_status_subj_idx"),
            # Covers the (target_module, subject_ref, target_kind) prefix AND the superseded lookup.
            models.Index(
                fields=["target_module", "subject_ref", "target_kind", "locator_hash"],
                name="enr_prop_target_locator_idx",
            ),
            # Cleanup beat (etap-10) / future partition by status/applied_at.
            models.Index(fields=["status", "applied_at"], name="enr_prop_status_applied_idx"),
        ]

    def save(self, *args, **kwargs) -> None:
        self.locator_hash = compute_locator_hash(self.target_locator)
        super().save(*args, **kwargs)

    def open_staged_file(self):
        """Open this proposal's staged binary (media — etap-08), returning a Django `File`.

        The decoupling seam (decision D1): a source-module adapter lives OUTSIDE this package
        (the PIM adapter is in `django-pim`) and must read the staged bytes without importing
        the bus's `staging_store`. So it calls `proposal.open_staged_file()` — the proposal
        carries its own opener. Raises `ValueError` if there is no staged file.
        """
        from django_enrichment.services import staging_store

        if not self.staged_file:
            raise ValueError(f"proposal {self.pk} has no staged_file")
        return staging_store.open_file(self.staged_file)

    def __str__(self) -> str:
        return f"ContentProposal({self.target_module}:{self.subject_ref}:{self.target_kind} [{self.status}])"
