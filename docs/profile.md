# Company profile and memory

Contract A4 § 2 and § 3. The **profile** is what the company states about itself (brand, audience, facts, tone,
language, rules); the **memory** is what the studio learned while working (SEO briefs, operator answers,
suggestions waiting for the operator). Both reach every AI prompt; neither is ever edited by the model.

## Profile

`Profile` — one row per scope: `channel` blank = the whole instance, otherwise a **channel overlay** that holds
only the sections that differ. `data` follows `schemas/profile_schema.json` (`PROFILE_SCHEMA`, draft 2020-12,
`schema_version`); every key is optional and an empty value means "missing":

```
{"brand": {"text", "differentiators": [str], "never_say": [str]},
 "persona": {"text", "personas": [{"name", "text"}]},
 "facts": [{"id", "label", "value", "source", "verified_at", "note"}],      # the facts registry
 "tone": {"text", "ai_patterns": [str]},
 "language": {"text", "spellings": [{"wrong", "right"}], "glossary": [{"term", "translations": {lang: str}}]},
 "headlines": {"text"}, "proof": {"text"}, "seo": {"text"}, "checklists": {"text"},
 "always_rules": [str],
 "rules": [{"id", "pattern", "flags", "message", "do", "dont", "active"}],   # deterministic lint (QA)
 "routing": {"<task type or kind>": ["brand", "tone", …]}}
```

Rule `pattern`s must compile as regular expressions. Validation reports every problem with its path
(`brand.differentiators.0`); the API turns them into one 400 detail per `data.<path>`.

**Versions.** `profile_service` is the only writer. Every save validates, bumps `version` and snapshots the data
as a `ProfileVersion` (`version`, `data`, `note`, `created_by`); restore = save an old snapshot as a new version.
`update_profile(channel, change)` applies a change under the row lock (memory promotion uses it).

**Effective profile.** `merged(channel)` = instance data with the overlay's non-empty sections on top. A
markdown section replaces the instance's; the list sections add to it — `always_rules` are concatenated
(deduplicated), `rules` and `facts` are joined by `id` with the overlay winning on the same id. A memory
promotion into a channel overlay therefore never hides the instance rules or facts.

**QA checks.** `rules`, `language.spellings`, the `facts` registry and `checklists.text` also drive the QA
warnings on every proposal revision — `docs/api.md` § QA checks.

### Prompt context — `profile_service.context_for(*, channel, task_type, kind) -> (instructions, data)`

| Step | Rule |
|---|---|
| sections | `routing[task_type]`, else `routing[kind]`, else the default: write → brand, persona, tone, language, facts; pick → brand, language; generate → brand, persona, tone, language, facts, proof, seo; questions → brand, facts; anything else → brand |
| order | facts → brand → the rest in routing order (conflict priority, stated in the instructions) |
| instructions | conflict note, `always_rules` (always, first), then each routed text section (`## Brand`, …) — operator-authored text is the instruction channel; it goes to the **system** message |
| data | the facts registry as a markdown table (`id, label, value, source, verified_at`) — a fenced `company_profile` data block in the **user** message |
| cap | 12 000 characters together; facts keep priority, the cut is logged and reported in the instructions (`[Company profile truncated: …]`) |

An empty profile gives `("", "")` — prompts then carry no profile section at all.

**Where it is used.** Kinds `write`, `pick` (through `seams.profile_for_task`), `generate`, `questions`
(`kinds/step.build_prompt`) and corrections (`revise_prompt.company_context`, routed like the worker's kind).
`EnrichmentTask.profile_versions` and `ProposalRevision.profile_versions` record the `ProfileVersion` ids
(instance, then overlay) behind the prompt; revision 1 copies its task's.

### Status — `profile_service.status(channel)`

Completeness of the effective profile for the CMS health view:

| Section | Minimum |
|---|---|
| brand | ≥ 1 differentiator |
| persona | ≥ 1 persona (or a text) |
| facts | ≥ 3 verified facts (`verified_at` set) |
| tone | ≥ 3 text lines or ≥ 1 AI pattern |
| language | ≥ 1 spelling rule or a text |

## Memory

`MemoryEntry`: `channel` (blank = instance), `subject_module` + `subject_ref` (blank = general; a category is a
subject too), `kind` `seo_brief | answer | rule_suggestion | fact_suggestion`, `title` ≤ 200, `text` ≤ 4000
(cut by `memory_service.remember`), `data`, `source` (`task:<id>` / `run:<uid>/<step>` / `proposal:<id>`),
`status` `active | dismissed | promoted`, `expires_at`, `created_by`. Indexed by
`(channel, subject_module, subject_ref, kind)`.

**Recall** — `memory_service.recall(*, channel, subject_module, subject_ref, kinds, budget_chars) -> str`: one line
per entry (`- [kind] title: text`, `data` as JSON when there is no text), the subject's entries first, then its
category's when the adapter has the optional extra `category_of(subject_ref, *, channel) -> str | None` (asked in the recall channel); newest first; only
`active`, unexpired entries of the instance or the given channel; whole lines up to the budget (a single
oversized first line is cut). Prompts use 3 000 characters (`seams.MEMORY_BUDGET_CHARS`):

| Prompt | Recalls |
|---|---|
| `write` / `pick` / corrections | `seo_brief` about the subject (then its category) — fenced `memory` block; skipped when the task runs the `seo_brief` augmentation (its own block carries it) |
| `generate` / `questions` steps | `seo_brief` about `run:<uid>` (module `enrichment`) |
| `questions` | also the general `answer`s (the only prompt that recalls them) (fenced `remembered_answers`) so it does not ask again |

**Writers**

| Kind | Written by |
|---|---|
| `answer` | approving a workflow `questions` step — one general entry per answered question (`record_answers`) |
| `rule_suggestion` | `POST proposals/{id}/rules/promote/` with `target: "profile"` — the profile itself is not edited |
| `seo_brief` | the `seo_brief` augmentation (contract A4 § 5) via `remember`, 90 days, subject or category, per language (`data.language`; `docs/api.md` § Augmentations) |
| `fact_suggestion` | QA "add to facts" (contract A4 § 4): `POST proposals/{id}/facts/suggest/` `{value, label?}` (`suggest_fact`) |

**Promotion** — `memory_promotion.promote(entry, target=None, user=None)`: `rule_suggestion` → `always_rules`
(default) or `rules` (the entry's `data` must carry `pattern`; `flags`, `do`, `dont` optional); `fact_suggestion`
→ `facts` (`verified_at` = today — promoting is the operator vouching for it). Only `active` entries; the
channel's profile gets a new version (`note: promoted memory <id>`) and the entry becomes `promoted` in the same
transaction. A promoted entry's status cannot change any more.

## API (admin v2, JWT + IsAdminUser, area `enrichment.profile`)

The profile is the operator's instruction channel, so editing it (and promoting memory into it) needs its own
area, narrower than reviewing proposals (`enrichment.proposals`).

| Method | Path | Notes |
|---|---|---|
| `GET` | `profile/?channel=` | `{channel, data, version, schema_version, updated_by_id, modified_at}` — the scope's own data (version 0 = never saved) |
| `PUT` | `profile/?channel=` | `{data, note?}` → 200 new version; schema errors → 400 with `details[].field = data.<path>` |
| `GET` | `profile/versions/?channel=` | `[{id, version, note, data, created_by_id, created_at}]`, newest first |
| `POST` | `profile/versions/{n}/restore/?channel=` | → 200 profile (new version); unknown `n` → 404 |
| `GET` | `profile/status/?channel=` | `{channel, complete, sections: [{key, complete, minimum}]}` of the merged profile |
| `GET` | `profile/schema/` | `{schema_version, json_schema}` |
| `GET` | `memory/?channel=&kind=&subject_ref=&status=` | paginated, newest first |
| `PATCH` | `memory/{id}/` | `{status: active\|dismissed}` → 200 entry |
| `DELETE` | `memory/{id}/` | → 204 |
| `POST` | `memory/{id}/promote/` | `{target?: always_rules\|rules\|facts}` → 200 `{entry, profile}`; wrong kind/target, not active or an invalid result → 400 |

`?channel=` blank (or absent) = the instance; at most 64 characters.
