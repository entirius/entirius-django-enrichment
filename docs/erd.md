---
title: "Enrichment: Database Diagrams"
description: "Auto-generated ER diagrams for the Enrichment module."
sidebar:
  badge:
    text: "Auto-gen"
    variant: "note"
---

:::caution[Auto-generated]
These diagrams are auto-generated from Django model introspection.
Do not edit. Run `make erd` in entirius-docker to regenerate.
:::

## Enrichment Bus

```d2 layout=elk
EnrichmentTask: {
  shape: sql_table
  style.fill: "#00ACC1"
  style.stroke: "#12141A"
  style.font-color: "#EBEDF2"
  id: int {constraint: primary_key}
  requested_by_id: int {constraint: foreign_key}
  type: varchar
  params: jsonb
  scope_spec: jsonb
  batch_key: varchar
  status: varchar
  counts: jsonb
}

ContentProposal: {
  shape: sql_table
  style.fill: "#00ACC1"
  style.stroke: "#12141A"
  style.font-color: "#EBEDF2"
  id: int {constraint: primary_key}
  task_id: int {constraint: foreign_key}
  reviewed_by_id: int {constraint: foreign_key}
  target_module: varchar
  target_type: varchar
  subject_ref: varchar
  subject_label: varchar
  subject_url: varchar
}

SpawnRule: {
  shape: sql_table
  style.fill: "#00ACC1"
  style.stroke: "#12141A"
  style.font-color: "#EBEDF2"
  id: int {constraint: primary_key}
  key: varchar {constraint: unique}
  module: varchar
  check_key: varchar
  params: jsonb
  scope: jsonb
  task_type: varchar
  task_params: jsonb
}

User: {
  shape: sql_table
  style.fill: "#484B57"
  style.stroke: "#1A1C25"
  style.stroke-dash: 3
  style.font-color: "#9A9CAA"
  id: int {constraint: primary_key}
  label: "User (External: auth)"
}



EnrichmentTask.requested_by_id -> User.id: {style.stroke: "#484B57"}

ContentProposal.task_id -> EnrichmentTask.id: {style.stroke: "#00ACC1"}

ContentProposal.reviewed_by_id -> User.id: {style.stroke: "#484B57"}
```
