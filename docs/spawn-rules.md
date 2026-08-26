---
title: Spawn Rules
description: Turn PIM quality gaps into enrichment tasks — manually or on a schedule, without duplicating work.
sidebar:
  label: Spawn Rules
  order: 2
---

A spawn rule maps one quality check to one work-type: "every product failing `pl-description`
becomes a `fix-attribute` task". Rules live in django-enrichment and run from two triggers — the
operator's **Run now** button (CMS → Enricher → Spawn Rules) and the `enrichment.gap_autospawn`
beat for rules with `auto` enabled.

The modules stay decoupled. A rule's `check_key` is a plain string naming the source module's own
rule (for PIM, a `GapDefinition` key) — no foreign key, no upfront validation. An unknown key
fails loudly at run time, raised by the module's adapter.

## The rule

| Field | Meaning |
|-------|---------|
| `key` | Stable slug, immutable after create. Drives the task `batch_key` (`gaprule:<key>`). |
| `module` | Adapter registry key (`pim`). |
| `check_key` | The module-side quality rule (PIM: `GapDefinition.key`). |
| `scope` | `{channel, language?}` — where to look for gaps. |
| `task_type` | Work-type the worker understands (`fix-attribute`, `fill-attribute`, `translate`). |
| `task_params` | Copied into the task's `params` (prompt hints for the worker). |
| `limit` | Max targets one task feeds the worker. `null` = `ENRICHMENT_SPAWN_DEFAULT_LIMIT` (200). |
| `cooldown_days` | How long a rejected/applied proposal keeps its target out of new tasks. `null` = `ENRICHMENT_SPAWN_COOLDOWN_DAYS` (7). |
| `auto` | Picked up by the beat. Off by default. |
| `active` | Master switch. |

## How a run works

1. **Dedup first.** An `open` or `in_progress` task with the rule's `gaprule:` batch key means a
   run is already in flight — you get it back (`already_running`), never a duplicate.
2. **Probe.** Page 1 of candidates is resolved through the filter below. Empty → `no_candidates`,
   no task is created.
3. **Spawn.** A gap-mode task is created; the worker resolves its targets lazily through the
   module adapter (PIM reads its materialised `GapFinding` rows — inherited gaps excluded, muted
   products absent).

The task is bounded by `limit`: once the worker has proposed that many targets, the queue reads
empty and the task closes. Gaps beyond the limit wait for the next run — with `auto` on, the beat
spawns the next batch once the previous one is done. Predictable portions in the review queue
instead of a 600k-proposal flood from one click.

## Why targets don't duplicate

Three layers, all server-side:

- **One task per rule** — the deterministic batch key plus the open/in-progress lookup.
- **Resolve-time filter** — a target with a `pending` or `drifted` proposal is skipped; a
  `rejected` one stays out for `cooldown_days` (no reject → regenerate → reject loop); an
  `applied` one too (a write that still fails the check must not burn LLM calls every pull).
- **Intake** — `external_ref` idempotency and `locator_hash` supersede, same as every other
  spawn surface.

## The worker contract (gap mode)

Gap-mode targets are a work queue: consumed targets drop off the front, so the worker re-pulls
**page 1 until it comes back empty** — incrementing the page would silently skip items. A failed
generation must be submitted as a low-confidence proposal carrying the error note; a silent skip
re-surfaces the same target on every pull, forever.

## Muting a genuine exception

Some products genuinely don't need what a rule demands. A **gap exemption** (PIM API:
`POST {channel}/gaps/exemptions/` with `{sku, definition_key, language?}`) is a deep mute: the
detection stops writing the finding, so the badge, the rollup counters and the enrichment
candidates all clear at once. Deleting the exemption re-detects and the gap comes back if still
real. `language` empty mutes every language of that rule for the product.

## API

```
GET/POST   /api/enrichment/v2/admin/spawn-rules/
GET/PATCH/DELETE /api/enrichment/v2/admin/spawn-rules/{key}/
POST       /api/enrichment/v2/admin/spawn-rules/{key}/run/   # 201 spawned | 200 already_running | 200 no_candidates
```

Admin JWT, `enrichment_spawn` throttle on `run`. `GET /tasks/?batch_key=gaprule:<key>` answers
"is this rule running" for UIs.
