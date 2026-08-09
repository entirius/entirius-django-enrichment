---
title: Adapter Guide
description: How to write an enrichment adapter — the contract, registration, and PIM as the reference implementation.
sidebar:
  label: Adapter Guide
  order: 1
---

The enrichment bus never touches a source module directly. It routes every read and write through an
**adapter** — a small module of functions that the source module owns. PIM is the first adapter;
ContentDB, blog, or anything else is the next one. This page is the recipe: read it, write an
adapter, register it, done. No change to the bus.

The dependency points one way: the bus depends on the contract, never on your module. Your adapter
imports nothing from `django-enrichment`. That is what makes "add ContentDB support" a new file
instead of a redesign.

## The Contract

An adapter is a Python **module** exposing duck-typed module-level functions. There is a documentary
`Protocol` (`django_enrichment.adapters.base.EnrichmentAdapter`), but you do not import or subclass
it — registration is by dotted path, and the bus calls the functions by name.

Five callables are the contract; a sixth is optional.

| Callable | Side | When the bus calls it |
|----------|------|-----------------------|
| `resolve_targets(scope_spec, page=1) -> list` | READ | A worker pulls a task's targets, one page at a time. |
| `find_gaps(check, params, scope) -> list` | READ | `scope_spec.mode == "gap"` — a named quality check finds candidates. |
| `read_current(*, subject_ref, target_kind, target_locator) -> dict` | READ | At intake (snapshot the truth) and again before apply (drift check). |
| `apply(proposal) -> None` | WRITE | After review, once the drift check passes. The only write path. |
| `revert(proposal) -> None` | WRITE | Single-level undo — restore `current_snapshot`. |
| `release_undo_anchor(proposal) -> int` | WRITE | Optional. Cleanup-time GC of an external undo anchor. |

The exact signatures live in `django_enrichment/adapters/base.py`. The bus calls `resolve_targets`
when a worker pulls targets, `read_current` at intake and again before apply (that second call is the
drift check), `apply` after the operator accepts, `revert` on undo, and `release_undo_anchor` when
retention prunes a proposal. `revert` and `release_undo_anchor` are only used by the undo and
cleanup flows — text-only adapters can skip `release_undo_anchor` entirely (the bus calls it via
`getattr`).

### Opaque values

The bus never reads `proposed_value` or `current_snapshot`. They are JSON blobs that only your
adapter interprets — same as `EnrichmentTask.params` is opaque to your module and read only by the
worker. PIM knows that a text proposal is `{"text": "..."}` and a picture proposal is
`{"op": "replace_main", "alt_t9n": {...}}`. The bus knows neither. This is what keeps
`ContentProposal` from growing a column per module: PIM's `feature_idx`, `channel`, and `language`
ride inside `target_locator`, not as table columns.

## Registration

The host service maps a `target_module` key to your adapter's dotted path:

```python
# settings — one entry per source module the client runs
ENRICHMENT_ADAPTERS = {
    "pim": "django_pim.services.enrichment_adapter",
    # "contentdb": "django_contentdb.services.enrichment_adapter",  # the next adapter
}
```

`adapters/registry.get_adapter(target_module)` reads `settings.ENRICHMENT_ADAPTERS` on **every call**
(a value snapshotted at import time would miss an adapter that self-registers after app load),
lazy-imports the dotted path, and caches the module by path. A missing key raises `ValueError` — a
client that does not run ContentDB simply has no entry, which is a configuration fact, not a crash.

`apply_service` routes by `proposal.target_module` to the registered adapter. The moment that routing
becomes `if target_module == "pim"`, the design has failed.

## PIM as Reference

The reference adapter is `django_pim/services/enrichment_adapter.py` — a module of functions, zero
`import django_enrichment`. PIM's locator and value convention (opaque to the bus, read only here):

```text
subject_ref      = product SKU
target_kind      = "attribute_value" (text) | "picture" (media)
target_locator   = {"channel", "language", "feature_idx"}   (text)
                 | {"channel"}                               (picture)
proposed_value   = {"text": <new value for one language>}   (text)
                 | {"op": "replace_main", "alt_t9n": {...}}  (picture)
```

### read_current — read the raw value, not a fallback

`read_current` is the source of truth for diff, drift, and undo. PIM reads the **raw**
`value_txt_t9n[language]`, never `get_value` (which falls back across languages). A fallback here
would make the drift check compare the wrong string and make undo restore the wrong one.

```python
def read_current(*, subject_ref: str, target_kind: str, target_locator: dict) -> dict:
    if target_kind == "picture":
        return _read_current_picture(subject_ref, target_locator)
    if target_kind != "attribute_value":
        return {"text": ""}
    channel_idx, language, feature_idx = _locator(target_locator)
    try:
        product = product_service.get_product_by_sku(channel_idx, subject_ref)
    except ObjectDoesNotExist:
        return {"text": ""}
    attr = ProductAttribute.objects.filter(product=product, feature__idx=feature_idx).first()
    if attr is None:
        return {"text": ""}
    raw = attr.value_txt_t9n or {}
    return {"text": raw.get(language) or ""}
```

Missing product, feature, or language all collapse to `{"text": ""}` so the bus can still diff and
detect drift. Your adapter decides what the snapshot shape is — text uses `{"text": ...}`, pictures
use a dict keyed by `sha1`. The bus compares snapshots with `!=` and never looks inside.

### apply — read-merge-write

PIM's write path replaces the whole `value_txt_t9n` dict per feature. Writing one language directly
would wipe the others, so `apply` (and `revert`) merge the proposed language into the full dict
before writing. The shared helper also guards the type — a non-string would corrupt the field for
every reader (storefront, Matrix sync, `get_value`), so it fails loud instead of persisting junk:

```python
def _write_text(channel_idx, *, subject_ref, feature_idx, language, text) -> None:
    if not isinstance(text, str):
        raise ValueError(f"enrichment_adapter expects a string value for {feature_idx!r}/{language!r}, ...")
    # ... resolve feature, reject non-t9n feature types ...
    existing = ProductAttribute.objects.filter(product=product, feature=feature).first()
    merged = dict(existing.value_txt_t9n or {}) if existing is not None else {}
    merged[language] = text
    product_service.update_product(channel_idx, subject_ref, attributes=[{"feature_idx": feature_idx, "value_txt_t9n": merged}])
    _mark_overridden_if_inheriting(product, feature, language)
```

### Inheritance override — a PIM-specific write-time concern

A write on a secondary (non-default) channel whose product inherits the feature would be clobbered by
the next materialisation. PIM marks the language in `overridden_langs` after writing — but only when
inheritance is actually enabled for that flag on a non-default channel. This is exactly the kind of
module-specific write-time rule the bus must not know about. It lives in the adapter:

```python
def _mark_overridden_if_inheriting(product, feature, language) -> None:
    if product.shop.is_default:
        return
    inherits = product.inherit_descriptions if feature.idx in DESCRIPTION_FEATURE_IDXS else product.inherit_attributes
    if not inherits:
        return
    # re-fetch (the write deleted+recreated the row) and add the language to overridden_langs
```

### Media — read staged bytes through the proposal, not the bus

For pictures, the binary is not in `proposed_value`. The bus stages it in its own store, and the
adapter reads it via `proposal.open_staged_file()` — a Django `File`. Your adapter never imports the
bus's `staging_store`; the proposal carries its own opener. PIM uploads the bytes (SHA1 dedup),
links the new main, and drops the old main **link** — keeping the old `Picture` row in the pool as
the undo anchor:

```python
def _apply_picture(proposal) -> None:
    # ... validate op == "replace_main" ...
    staged = proposal.open_staged_file()
    try:
        picture = product_picture_service.upload_picture(staged)   # SHA1 dedup
    finally:
        staged.close()
    product = product_service.get_product_by_sku(channel_idx, sku)
    # Drop the old main LINK only — the Picture stays as the undo anchor.
    ProductPicture.objects.filter(product=product, picture_role=PictureRoleEnum.MAIN).delete()
    product_picture_service.link_picture_to_product(channel_idx, sku, picture.pk, picture_role="main", ...)
```

### release_undo_anchor — reference-guarded GC

When retention expires and the bus prunes a media proposal, the displaced `Picture` should be
collected — but PIM's SHA1 dedup means the same image may be used elsewhere. So the GC scans **every**
reverse relation and keeps the picture if any non-owned one still references it. A future model that
references `Picture` cannot silently make this unsafe — it would show up as a reverse relation and
block the delete:

```python
def _gc_orphan_picture(sha1: str) -> int:
    # ... look up Picture by sha1 ...
    for rel in picture._meta.related_objects:
        accessor = rel.get_accessor_name()
        if accessor in _OWNED_PICTURE_RELATIONS:   # its own thumbs / download-urls cascade — ignore
            continue
        if getattr(picture, accessor).exists():
            return 0                                # still displayed somewhere — keep it
    Picture.objects.filter(pk=picture.pk).delete()  # zero external references — reclaim
    return 1
```

This is optional: a text adapter keeps no external anchor (the snapshot lives in the proposal row),
so it can skip `release_undo_anchor` and the bus's `getattr` simply finds nothing to call.

## Gotchas

Five rules the bus relies on you to honour:

- **Values are opaque to the bus.** `proposed_value` and `current_snapshot` are yours alone. Do not
  expect the bus to validate or interpret them.
- **`read_current` reads the live, raw value.** No cross-language fallback, no cached copy. Drift and
  undo are only correct if this returns ground truth.
- **Snapshot, drift, and undo semantics belong to the adapter.** PIM materialises inheritance at
  write time; another module may version its own way. The bus stays dumb.
- **Read staged binaries via `proposal.open_staged_file()`.** Never import the bus's `staging_store`.
  The seam keeps the dependency one-directional.
- **Cross-module access goes through services, never models.** Your adapter calls
  `product_service.get_product_by_sku(...)`, not `Product.objects.get(...)` from another module.

## New-Adapter Checklist

To add a source module to the bus:

1. Create `services/enrichment_adapter.py` in the source module. Implement `resolve_targets`,
   `find_gaps`, `read_current`, `apply`, `revert` (and `release_undo_anchor` if your targets keep an
   external undo anchor). Import nothing from `django-enrichment`.
2. Decide your opaque conventions: what `target_locator`, `proposed_value`, and `current_snapshot`
   look like for each `target_kind` you support. Document them in the module docstring, as PIM does.
3. Register the dotted path under a key in `ENRICHMENT_ADAPTERS`.
4. Optional: add a CMS diff renderer for any new `target_kind` (text, picture, and JSON fallback
   already exist in the review queue).

That is the whole surface. The bus, `GapDefinition`, `resolve`, and `apply_service` do not change —
the pattern grows by adding adapters, not by rewriting the consumer. ContentDB is the intended second
adapter and follows this same recipe. For the cross-module pattern behind it — quality flags,
`GapDefinition`, and how candidates map onto proposals — see
the PIM [Quality Gaps](/volkanos/modules/pim/quality/) docs.
