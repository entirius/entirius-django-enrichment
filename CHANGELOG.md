# Changelog

## 2.1.0 — 2026-08-27

- Optional `on_reject` adapter hook on both reject paths (`adapters/base.py`,
  `proposal_service`). A proposing module can now learn that a human turned its
  proposal down, instead of re-proposing the same pair once the cooldown lapses.
  The hook is fetched with `getattr(adapter, "on_reject", None)`, so adapters that
  do not implement it are unaffected and `bulk_reject` stays a single UPDATE for
  every module without one.
  `django_atlas.services.enrichment_adapter` implements it for the
  `duplicate_in_pim` acceptance queue, feeding `dedup_log.rejected_pairs`.

## 2.0.0 — 2026-07-11

- Initial public release: the horizontal content-quality bus —
  accept-before-write enrichment for product content. Models, service layer,
  the PIM adapter, spawn rules and surfaces, media proposals, scale/undo,
  and the v2 admin API.
- Migrations squashed into a single initial migration for the Entirius epoch.
