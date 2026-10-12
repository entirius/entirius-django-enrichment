---
title: Management Commands
description: Retention prune, demo seed and the Celery beat schedule of the enrichment module.
---

| Command | What it does |
|---------|-------------|
| `python manage.py prune_enrichment` | Retention prune: delete terminal proposals + done tasks older than `ENRICHMENT_RETENTION_DAYS`, drop staged files, GC displaced PIM `Picture`s. `--days N` overrides the window (`--days 0` prunes everything qualifying). Same work the daily beat does. |
| `python manage.py seed_demo_proposals` | Seed demo enrichment proposals (text + picture) onto the review queue. Needs the `demo` extra (Pillow, placeholder images). |

**Celery beat:** the host schedules `enrichment.prune` daily at 02:00 UTC and
`enrichment.gap_autospawn` every 30 min (runs every `auto=True, active=True` SpawnRule;
per-rule batch_key dedup + page-1 probe make an idle pass one cheap query per rule), both on the
`enrichment` queue
(`CELERY_BEAT_SCHEDULE` in the service settings). One knob `ENRICHMENT_RETENTION_DAYS` (default 30) =
undo window = retention = storage upper bound. Full design: `docs/scale-and-undo.md` § Retention.
