# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.conf import settings

# Adapter registry (target_module -> adapter). Populated in etap-03 (decision #11).
# The enrichment bus routes apply/snapshot/resolve_targets by `target_module` to the
# registered adapter; the first adapter ("pim") is wired in a later stage.
ENRICHMENT_ADAPTERS = getattr(settings, "ENRICHMENT_ADAPTERS", {})

# Media staging (etap-08). The bus stages a proposal's binary in its OWN store; PIM stays
# untouched until accept. These are read at call time via `getattr(django.conf.settings, ...)`
# (see staging_store / proposal_service), NOT snapshotted here — a host override or a test
# `settings` fixture must win. The names below are the documented defaults only.
#
# ENRICHMENT_STAGING_DIR     — filesystem root for staged binaries (default: <MEDIA_ROOT>/enrichment_staging)
# ENRICHMENT_MAX_IMAGE_BYTES — hard cap rejected at intake (25 MB)
# ENRICHMENT_ALLOWED_IMAGE_MIME — magic-byte allowlist enforced at intake
ENRICHMENT_MAX_IMAGE_BYTES = getattr(settings, "ENRICHMENT_MAX_IMAGE_BYTES", 25 * 1024 * 1024)
ENRICHMENT_ALLOWED_IMAGE_MIME = getattr(
    settings,
    "ENRICHMENT_ALLOWED_IMAGE_MIME",
    frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}),
)

# Mass apply / undo (etap-09). Same getattr-at-call-time pattern as the media settings — a host override
# or a test `settings` fixture wins; the values here are the documented defaults.
#
# ENRICHMENT_ASYNC_APPLY_THRESHOLD — bulk_accept/bulk_undo run sync in-request at or below this many
#                                    proposals (immediate counts); above it they fan out to the canvas.
# ENRICHMENT_APPLY_BATCH_SIZE      — Celery chunk size for the canvas (bounds per-chunk memory).
# ENRICHMENT_CELERY_QUEUE          — dedicated queue for heavy apply/undo; the host wires a worker on it.
ENRICHMENT_ASYNC_APPLY_THRESHOLD = getattr(settings, "ENRICHMENT_ASYNC_APPLY_THRESHOLD", 50)
ENRICHMENT_APPLY_BATCH_SIZE = getattr(settings, "ENRICHMENT_APPLY_BATCH_SIZE", 500)
ENRICHMENT_CELERY_QUEUE = getattr(settings, "ENRICHMENT_CELERY_QUEUE", "enrichment")

# Cleanup / retention (etap-10). ONE knob: the undo window = the retention window = the storage
# upper bound. The daily beat (`tasks/cleanup.prune_task`) prunes settled proposals + done tasks
# older than this many days, drops their staged binaries, and GCs the displaced PIM `Picture` (the
# undo anchor) once nothing else references it. After X days undo expires; the live value stays in
# the source module (that's content). Read at call time, same as the settings above.
ENRICHMENT_RETENTION_DAYS = getattr(settings, "ENRICHMENT_RETENTION_DAYS", 30)

# CSV import spawn (etap-11). Hard cap rejected at upload — format is validated, SKU existence is
# resolved lazily, so even a 50k-row file is cheap; the cap only stops an abusive upload. Read at
# call time (host/test override wins), same pattern as the settings above.
ENRICHMENT_MAX_CSV_BYTES = getattr(settings, "ENRICHMENT_MAX_CSV_BYTES", 10 * 1024 * 1024)

# Gap spawn (etap-13). Defaults for SpawnRule rows whose `limit` / `cooldown_days` are NULL —
# read at call time (host/test override wins), same pattern as the settings above.
#
# ENRICHMENT_SPAWN_DEFAULT_LIMIT  — max targets one gap task feeds the worker (budget guard;
#                                   the beat spawns the next batch once the previous is done).
# ENRICHMENT_SPAWN_COOLDOWN_DAYS  — how long a rejected/applied proposal keeps its target out of
#                                   new gap candidates (breaks the reject→respawn→reject loop).
ENRICHMENT_SPAWN_DEFAULT_LIMIT = getattr(settings, "ENRICHMENT_SPAWN_DEFAULT_LIMIT", 200)
ENRICHMENT_SPAWN_COOLDOWN_DAYS = getattr(settings, "ENRICHMENT_SPAWN_COOLDOWN_DAYS", 7)
