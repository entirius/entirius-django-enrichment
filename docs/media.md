---
title: Media Proposals
description: Picture proposals on the enrichment bus — accept-before-write for binaries.
---

The second `target_kind` on the bus: **`picture`**. Same accept-before-write loop as text, with one
hard rule — the binary never touches the target module until an operator accepts.

## Why a staging-store

The worker (n8n + ComfyUI) is fire-and-forget: it generates bytes and posts them, then forgets.
It hosts nothing. PIM, on the other hand, only stores a picture as a real `Picture` row (file + SHA1)
the moment one exists. If the bus wrote straight to PIM at intake, 10k pending proposals would mean
10k files materialised in PIM for content that might be rejected — and every reject would leave an
orphan to GC.

So the bus keeps the binary in its **own** store (`services/staging_store.py`) until accept:

- **pending** = staged file only. PIM untouched. 10k pending = 10k rows + 10k staged files, zero in PIM.
- **reject** = delete the staged file. PIM has zero trace, no orphans.
- **accept** = hand the staged bytes to the adapter, which uploads them to PIM; the bus then drops
  the staged copy. The displaced old `Picture` (not the staged file) is the undo anchor.

`staged_file` on `ContentProposal` holds the opaque ref. The store is a thin function module
(`save` / `open_file` / `delete` / `exists`) over a filesystem root (`ENRICHMENT_STAGING_DIR`). The
interface is the stable seam — an S3 backend can replace the body later (future finding). Refs are
flat `<uuid>.<ext>` filenames; `open_file`/`delete`/`exists` reject any ref with a path separator or
`..` (traversal guard).

## Intake (multipart)

`POST /api/enrichment/v2/admin/proposals/upload-media/` — `multipart/form-data`:

- `file` — the binary (the only file part).
- `subject_ref`, `target_locator` (JSON string, e.g. `{"channel":"default-europe"}`),
  `proposed_value` (JSON string, `{"op":"replace_main","alt_t9n":{...}}`), plus the usual
  `source` / `external_ref` / `confidence` / `batch_id` / `task_id` metadata.

`target_kind` is forced to `"picture"`. The service (`proposal_service.intake_media`) validates the
file at the boundary — **size cap** (`ENRICHMENT_MAX_IMAGE_BYTES`, 25 MB) and a **magic-byte MIME
allowlist** (`ENRICHMENT_ALLOWED_IMAGE_MIME`: jpeg/png/webp/gif/avif). The MIME check sniffs the
actual leading bytes, never the client-declared `content_type` (which is spoofable). Throttled by the
same `enrichment_intake` scope as text intake.

`current_snapshot` is read authoritatively from the adapter at intake (the displaced main, or `{}`).

## Review (CMS)

`DiffRenderer` maps `target_kind=image|picture` to `ImageDiff` (before/after thumbnails, opening the
reused `SupplierReview/GalleryModal` for fullscreen):

- **before** = the current PIM main — `current_snapshot.url` (a public `/media/...` path), prefixed
  with the backend base.
- **after** = the staged binary, served by `GET /proposals/{id}/staged-file/` (admin-only). The CMS
  fetches it as a blob through the authenticated client and objectURLs it — an `<img src>` can't
  carry the Bearer token.

## Apply / revert (PIM adapter)

In `django-pim` (`services/enrichment_adapter.py`), `op=replace_main`:

1. `proposal.open_staged_file()` → the staged bytes (the adapter never imports the bus's
   `staging_store`; the proposal carries its own opener — decision D1).
2. `product_picture_service.upload_picture(file)` — SHA1 dedup → `Picture`.
3. Drop the old main `ProductPicture` link (the `Picture` row stays in the pool).
4. `link_picture_to_product(..., picture_role="main", position=0, language_iso2=None,
   alt_text_t9n=proposed_value["alt_t9n"])`.

One main per `(subject_ref, channel)`; languages ride in `alt_text_t9n` (link `language=None`), so the
picture locator carries no language.

`revert` (single-level undo): look the old `Picture` up by `current_snapshot["sha1"]` (still in the
pool — GC is etap-10), drop the current main, re-link the old one. Empty snapshot → just drop the
current main.

## Lifecycle of the staged file

| event | staged file |
|-------|-------------|
| intake | created |
| reject / bulk-reject | deleted |
| accept (applied) | deleted (binary now in PIM; old `Picture` is the undo anchor) |
| drift | kept (operator re-confirms, then applies) |

## Out of scope (future)

`target_kind=gallery` (reorder manifest), `ProductVideo`, async mass apply of 10k+ (etap-09), GC of
the orphaned old `Picture` (etap-10), S3 staging backend, operator-authored alt text in the CMS.
