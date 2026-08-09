---
title: Enrichment
description: Horizontal content-quality bus — accept-before-write enrichment for product content.
sidebar:
  label: Overview
  order: 0
  collapsed: true
---

:::caution[In development]
The bus is built: models, service layer, the PIM adapter, the admin API, and the CMS review/spawn UI all exist. Still missing is the n8n worker that generates proposals — until it ships, proposals are created by hand (API/shell) for testing.
:::

django-enrichment is a horizontal content-quality bus. It improves existing product content —
descriptions, attributes, SEO, media — through an accept-before-write loop, so nothing reaches live
data without an operator's review.

## What It Does

- The CMS spawns a typed `EnrichmentTask` (e.g. "regenerate description", "translate", "enrich media") over a scope of products.
- An external worker (n8n) pulls open tasks, generates content, and posts back `ContentProposal` rows — one per field.
- An operator reviews proposals in a universal CMS queue and accepts or rejects them in bulk.
- Accepted proposals write back to the source module through a per-module adapter — PIM first, others later.

## Two Entities

The whole bus is two models. A task is a work order; a proposal is a result waiting for review.

- **`EnrichmentTask`** — a thin work order. One row per command, not per target: targets are resolved
  lazily, so the table grows with commands, not with the catalogue. Its lifecycle is deliberately
  coarse — `open → done`. No lease, retry, or DAG. The moment it needs those, that is a workflow
  engine, a separate module, not this one.
- **`ContentProposal`** — one row per proposed field change. Its target is **generic from day one**:
  `target_module` keys the adapter, and module specifics (PIM's `feature_idx`, `channel`, `language`)
  live in a `target_locator` JSON blob, not in table columns. `proposed_value` and `current_snapshot`
  are opaque — only the source module's adapter reads them.

One task fans out to many proposals. The proposal table is a working set of in-flight edits, not a
copy of the catalog — it stays small while PIM stays untouched until accept. See the
[database diagrams](/volkanos/modules/enrichment/erd/) for the full schema.

## Why a Separate Module

Enrichment is a bus, not a satellite of PIM. Because the target is generic from day one, adding
ContentDB or blog support means **writing an adapter, not redesigning the bus**. The bus owns tasks,
proposals, the state machine, review, intake, batch apply, and undo — and knows nothing about any
source module. That knowledge lives in an adapter, registered under a key in `ENRICHMENT_ADAPTERS`.

## Where It Lives

- **Module:** `django-enrichment` (this package) — the bus: tasks, proposals, state machine, review/intake API, batch apply, undo, retention.
- **Adapter:** lives in the source module (`django-pim` first), registered under a key in `ENRICHMENT_ADAPTERS`. The bus never imports PIM directly — the dependency points one way.
- **Worker:** n8n — a dumb puller. No selection logic; it pulls typed tasks, generates, and returns results.

## Review in the CMS

The operator works from the **Enricher** panel (CMS Blueprint). It has two surfaces.

**Spawn — where the data lives.** In PIM, select products (or filter the whole catalog) and use the "Send to enrichment" bulk action; a single product has the same button on its detail page. The spawn dialog picks the operation (translate / fix / fill), the target field, the languages, and one or more channels. One task is created per channel; the worker fans out one proposal per language. The button only appears when the Enricher panel is enabled.

**Review — one universal queue.** The `Enrichment Review` queue shows proposals from every module in one table: subject (deep-linked to its editor), module, field, a before/after diff, status, confidence, source, batch, age. Filter by status, module, kind, batch, source, confidence, or search; accept/reject a row, or accept/reject the whole filtered set in bulk.

- **Two modes.** *List* is the default — a table with bulk actions. *Focus* shows one proposal at a time with a large diff and keyboard shortcuts (`a` accept, `r` reject, `s` skip, arrows navigate) for careful review of a filtered subset.
- **Drift re-confirm.** If the live value changed after a proposal was generated, accepting it does not overwrite. The proposal flips to `drifted` and the operator is shown the proposal against the *new* current value — accept again to apply, or reject. No silent clobber.

## Extending the Bus

The pattern grows by adding adapters, not by rewriting the bus.

- [Adapter Guide](/volkanos/modules/enrichment/adapter-guide/) — the recipe: the contract, registration, and PIM as the reference implementation. Read it, write an adapter for your module, register it.
- [Gaps Per Module](/architecture/gaps-per-module/) — the cross-module pattern behind it: quality flags, gap definitions, and how candidates map onto proposals.
