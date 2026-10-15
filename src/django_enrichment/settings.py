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

# Product video (Amendment A1). Read at call time (host/test override wins), same pattern as above.
#
# ENRICHMENT_MAX_VIDEO_BYTES     — media intake cap for `target_kind="video"` (mp4 only, magic-byte sniffed).
# ENRICHMENT_VIDEO_TIMEOUT_S     — how long the `video` kind polls a toolbox media job inside the Celery task.
# ENRICHMENT_VIDEO_TOOLBOX_URL / _API_KEY / _CHANNEL — media job endpoint; each blank → the matching
#                                  AI_TOOLBOX_* setting. POC escape hatch (Redmine #34464).
ENRICHMENT_MAX_VIDEO_BYTES = getattr(settings, "ENRICHMENT_MAX_VIDEO_BYTES", 100 * 1024 * 1024)
ENRICHMENT_VIDEO_TIMEOUT_S = getattr(settings, "ENRICHMENT_VIDEO_TIMEOUT_S", 900)

# Batch correction (contract A3 § 1). Read at call time (host/test override wins), same pattern as above.
#
# ENRICHMENT_REVISE_BATCH_MAX   — most pending proposals one batch correction may cover (one paid completion each).
# ENRICHMENT_REVISE_LOCK_SECONDS — how long a task's batch lock outlives a worker that died mid-batch.
ENRICHMENT_REVISE_BATCH_MAX = getattr(settings, "ENRICHMENT_REVISE_BATCH_MAX", 200)
ENRICHMENT_REVISE_LOCK_SECONDS = getattr(settings, "ENRICHMENT_REVISE_LOCK_SECONDS", 3600)

# Run lanes (plan 22). Read at call time (host/test override wins), same pattern as above.
#
# ENRICHMENT_INTERACTIVE_QUEUE   — Celery queue of `lane=now` runs, workflow steps and corrections (a click waits on it).
# ENRICHMENT_BULK_QUEUE          — Celery queue of `lane=queue` runs; the host wires a worker with concurrency 1 on it.
# ENRICHMENT_RUN_NOW_MAX_SUBJECTS — most subjects one `lane=now` run may carry.
ENRICHMENT_INTERACTIVE_QUEUE = getattr(settings, "ENRICHMENT_INTERACTIVE_QUEUE", "enrichment_interactive")
ENRICHMENT_BULK_QUEUE = getattr(settings, "ENRICHMENT_BULK_QUEUE", "enrichment_bulk")
ENRICHMENT_RUN_NOW_MAX_SUBJECTS = getattr(settings, "ENRICHMENT_RUN_NOW_MAX_SUBJECTS", 20)

# Work queue (plan 23). Every knob is read at call time through `queue_config.get` (host/test override wins).
#
# ENRICHMENT_UNIT_SIZE              — subjects per work unit (small on purpose: a lost lease costs few paid calls).
# ENRICHMENT_MAX_INFLIGHT_PER_WORKER — leases one worker may hold at once, per lane.
# ENRICHMENT_LEASE_S                — lease length; a heartbeat extends it by the same amount.
# ENRICHMENT_MAX_ATTEMPTS           — claims of one unit before it fails for good.
# ENRICHMENT_RETRY_BACKOFF_S        — wait before a retried unit is claimable again (× attempt).
# ENRICHMENT_INSTANCE_ONLINE_S      — an instance is online when seen within this many seconds.
# ENRICHMENT_STALE_QUEUE_S          — queued longer than this with no online instance → "nobody is taking work".
# ENRICHMENT_PAUSE_AFTER_FAILURES   — consecutive failed units of one worker that pause it.
# ENRICHMENT_AUTO_PAUSE             — False = only notify, never pause.
# ENRICHMENT_BUDGET_ERROR_CODES     — toolbox error codes that pause the worker at once (budget / hard cap).
# ENRICHMENT_NOTIFY_CHANNEL         — django_notifications channel idx for escalations (missing → log only).
# ENRICHMENT_NOTIFY_ROLE            — recipient role of escalations.
# ENRICHMENT_PLAN_BATCH             — units created per insert while planning a task.
# ENRICHMENT_EVENTS_CAP             — events kept per unit.
# ENRICHMENT_CONSUME_MAX_UNITS      — units one consume run handles before re-queueing itself (bounds a Celery slot).
ENRICHMENT_UNIT_SIZE = 5
ENRICHMENT_MAX_INFLIGHT_PER_WORKER = 2
ENRICHMENT_LEASE_S = 300
ENRICHMENT_MAX_ATTEMPTS = 3
ENRICHMENT_RETRY_BACKOFF_S = 60
ENRICHMENT_INSTANCE_ONLINE_S = 120
ENRICHMENT_STALE_QUEUE_S = 900
ENRICHMENT_PAUSE_AFTER_FAILURES = 5
ENRICHMENT_AUTO_PAUSE = True
ENRICHMENT_BUDGET_ERROR_CODES = ("BUDGET_EXCEEDED", "HARD_CAP_REACHED", "SPEND_CAP_EXCEEDED", "QUOTA_EXCEEDED")
ENRICHMENT_NOTIFY_CHANNEL = "default-europe"
ENRICHMENT_NOTIFY_ROLE = "operator"
ENRICHMENT_PLAN_BATCH = 200
ENRICHMENT_EVENTS_CAP = 20
ENRICHMENT_CONSUME_MAX_UNITS = 10
