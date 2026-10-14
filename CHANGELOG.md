# Changelog

## Unreleased

- AI workers: `Worker` model (Django admin only, migration `0004_worker`), kind registry (`register_kind`) with the
  base kinds `translate` and `field`, toolbox calls on `django_utils.toolbox.ToolboxClient`, and an internal Celery
  runner (`enrichment.run_task`) dispatched on commit when an active internal Worker owns the task type.
- API: `GET workers/`, params validation on `POST tasks/` when a Worker owns the type, `PATCH proposals/{id}/`
  (pending only).
- Product video (Amendment A1): `intake_media(target_kind="video")` accepts mp4 (magic bytes,
  `ENRICHMENT_MAX_VIDEO_BYTES`); kind `video` (adapter `main_picture` → toolbox media job → one staged video
  proposal per subject), polling up to `ENRICHMENT_VIDEO_TIMEOUT_S`; `ENRICHMENT_VIDEO_TOOLBOX_URL/_API_KEY/_CHANNEL`
  (default `AI_TOOLBOX_*`, POC). `ProposalDraft.media_file` routes a draft to `intake_media`.
- API: per-product reads for the CMS product page — `GET proposals/?subject_ref=`, `GET tasks/?subject_ref=`
  (list-mode refs) `&target_module=`, `tasks/?status=` takes a comma list. `task_service.list_tasks` takes a
  `TaskFilters` dataclass.
- Failure reasons: `EnrichmentTask.errors` (migration `0005_task_errors`) — the runner records
  `{subject_ref, code, error_id}` per failed subject (toolbox error code via `toolbox_calls.failure_code`,
  never exception text, capped at 20). Task responses add `errors` and `proposal_counts`.
- Dependencies: `httpx` (video job calls), `entirius-django-utils>=2.1.0` (toolbox client), `nh3` (HTML sanitising); `respx` in the `test` extra.

## 2.2.0 — 2026-10-12

- Access: the module declares its own access areas on its AppConfig and its admin views (copied from the
  entirius-django-access defaults; behaviour unchanged).

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
