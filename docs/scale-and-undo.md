# Scale & Undo (etap-09)

Two things the bus needs before a real operator accepts at catalogue scale: a way back (undo) and a
way to apply 10k proposals without melting a worker (mass apply). Both hang off the same choke point —
`apply_service` is the only write into a source module, `undo_service` is the only way back.

## Single-level undo

`undo_service.revert(proposal)` restores the value `apply_service` captured *before* it wrote — one
step back, no deeper. That's deliberate: full history is `django-history`'s job (a separate track),
not this module's. `applied → reverted` is terminal; re-applying means a fresh proposal.

It's module-agnostic the same way apply is — it routes by `proposal.target_module` to the adapter's
`revert()`. The PIM adapter (in `django-pim`) restores text via read-merge-write (+ inheritance
override) and pictures by re-linking the old `Picture` (kept in the pool until the etap-10 cleanup
beat GC-s it). The bus never knows which.

### Drift-blocked undo

Undo refuses to clobber a change made *after* our apply. The mechanism is `applied_snapshot`: right
after the adapter writes, `apply_service` re-reads the live value through the adapter and stores it on
the proposal. That's the post-apply truth.

Before reverting, `undo_service` re-reads the live value and compares it to `applied_snapshot`:

- **match** → nobody touched it since we applied → restore `current_snapshot`, flip to `reverted`.
- **mismatch** → someone edited the target after our apply → raise `UndoBlockedError`, write nothing.

Why a stored snapshot rather than comparing against `proposed_value`? For text they're equal, but a
picture's `proposed_value` never holds the resulting sha1 — `read_current` does. Storing the adapter's
own post-write read keeps the drift check generic across every `target_kind` with zero per-module
branching.

In bulk undo, a blocked item lands in the `blocked` bucket and stays `applied` (the operator sees it
in the toast); it is never silently overwritten.

## Mass apply / undo — the Celery canvas

`bulk_accept` / `bulk_undo` route by size (`ENRICHMENT_ASYNC_APPLY_THRESHOLD`, default 50):

- **≤ threshold** → run the `proposal_service` core in-request, return counts inline
  (`mode: "sync"`). The operator gets immediate applied/reverted/drifted/blocked lists.
- **> threshold** → fan out to the canvas (`mode: "async"`, `enqueued: N`). The worker logs the
  totals; the UI re-fetches.

The canvas (`tasks/apply_tasks.dispatch_canvas`) splits the id list into `ENRICHMENT_APPLY_BATCH_SIZE`
(default 500) chunks. Each chunk is a `apply_chunk_task` (apply or revert, by `op`); a `chord`
callback (`finalize_batch_task`) sums the per-chunk result lists into batch totals once the group
finishes — "counts via chords". Per-chunk memory is bounded because the `proposal_service` core
streams with `.iterator()` instead of materialising the whole chunk (research R3: `bulk_update`/large
querysets eat memory; batch with `iterator()`).

```
bulk_accept (>50)
   └── dispatch_canvas(ids, op="apply")
         ├── chunk [0:500]   → apply_chunk_task → apply_many (.iterator())  ┐
         ├── chunk [500:1000]→ apply_chunk_task → apply_many (.iterator())  ├─ chord
         └── chunk [1000:..] → apply_chunk_task → apply_many (.iterator())  ┘
               └── finalize_batch_task(results) → {applied, drifted, failed}
```

Heavy apply runs on a dedicated queue (`ENRICHMENT_CELERY_QUEUE`, default `enrichment`) so it never
starves the default queue — like the suppliers image queue. The host service must run a worker that
consumes it (`celery -A main worker -Q ...,enrichment`).

### Idempotency

Re-running a bulk over already-applied proposals is safe: `apply_service.apply` rejects any status
outside `{pending, drifted}`, so a second pass drops every already-applied id into the `failed`
bucket — no double-write. Drift inside a batch is isolated per item (`drifted` bucket); it never
aborts the batch.

## Settings

| Setting | Default | Meaning |
|---------|---------|---------|
| `ENRICHMENT_ASYNC_APPLY_THRESHOLD` | `50` | At/below: sync in-request. Above: Celery canvas. |
| `ENRICHMENT_APPLY_BATCH_SIZE` | `500` | Chunk size — bounds per-chunk memory. |
| `ENRICHMENT_CELERY_QUEUE` | `"enrichment"` | Dedicated queue for heavy apply/undo. |

All read via `getattr(django.conf.settings, ...)` at call time — a host override or a test fixture
wins.

## Boundary

Single-level undo only. Multi-level history, rollback chains, and `bulk_update`-trigger capture are
`django-history` (separate track). Undo expires with retention: once the etap-10 cleanup beat GC-s the
old PIM `Picture` (or prunes the proposal), the way back is gone — the live value stays in PIM (that's
the content), only the ability to undo disappears.

## Retention (etap-10)

One knob — **`ENRICHMENT_RETENTION_DAYS` (default 30)** — is the undo window, the retention window,
and the storage upper bound all at once. The table and the staging store hold roughly `X` days of
*activity*, not a copy of the catalogue. A daily Celery beat (`enrichment.prune`, 02:00 UTC, on the
`enrichment` queue) calls `cleanup_service.prune`, which:

- **Deletes terminal proposals older than `X`.** `applied` aged by `applied_at` (the
  `(status, applied_at)` index serves it); `rejected` / `superseded` / `drifted` / `reverted` aged by
  `modified_at`. **`pending` is never touched** — that's live review work.
- **Drops each pruned media proposal's staged file** physically (`staging_store.delete`, idempotent).
- **GCs the displaced PIM `Picture`** (the undo anchor) via `adapter.release_undo_anchor` — but only
  when nothing else references it (SHA1 dedup is shared across products/attributes/categories; a
  Picture displaced from one product's main may still be in use elsewhere — never delete someone
  else's image). Text proposals carry no external anchor, so this no-ops for them.
- **Deletes `done` tasks older than `X`.** Their proposals survive (`task` FK is `SET_NULL`) — only
  the work-order row goes.

Out-of-band and idempotent: a re-run finds nothing, `staging_store.delete` no-ops on a missing ref,
and the adapter GC is reference-guarded — safe to run as often as you like.

Run it manually with `python manage.py prune_enrichment` (`--days N` overrides the window for a
one-off; `--days 0` prunes everything currently qualifying).

| Setting | Default | Meaning |
|---------|---------|---------|
| `ENRICHMENT_RETENTION_DAYS` | `30` | Undo window = retention = storage upper bound (one knob). |

When `django-history` lands (deeper history, separate track), `X` can shrink — the bus only needs to
keep undo alive for the single-level window.
