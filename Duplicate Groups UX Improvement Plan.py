# Databricks notebook source
# DBTITLE 1,Title
# MAGIC %md
# MAGIC # Duplicate Groups — UX Improvement Plan
# MAGIC
# MAGIC The UC Duplicate Data Detector currently surfaces duplicate groups across a Unity Catalog estate of 143K+ tables. While the detection engine is mature — combining column Jaccard similarity, type compatibility, fuzzy name matching, and transitive lineage scoring — the **Duplicates page itself is hard to navigate at scale**.
# MAGIC
# MAGIC Users land on a paginated list of 1,000s of groups with limited tools for prioritisation. Most groups are informational (pipeline stages, governance views) rather than actionable, and the current filter set does not go far enough to surface the groups that warrant real attention.
# MAGIC
# MAGIC This plan covers two areas:
# MAGIC 1. **Targeted filtering and display improvements** — changes to `applyGroupFilters`, `renderDuplicates`, and `renderDupGroupCard` that can be shipped incrementally.
# MAGIC 2. **Schema and dataset-level duplication detection** — higher-effort work that addresses the root problem at a higher level of abstraction.

# COMMAND ----------

# DBTITLE 1,Section 1: Current Filtering Limitations
# MAGIC %md
# MAGIC ## 1. Current Filtering Limitations
# MAGIC
# MAGIC The current filter set in `state.filters` covers five boolean or string values. The table below summarises what each filter does and the gaps it leaves.
# MAGIC
# MAGIC | Filter | Mechanism | What it does | Gap |
# MAGIC |---|---|---|---|
# MAGIC | Hide governance views | Tag-based (`governance_view`) | Hides 2-table groups where a VIEW's columns are a subset of the paired TABLE | No way to show *only* governance views, or to filter on view proportion within a group |
# MAGIC | Hide pipeline stages | Tag-based (`pipeline_stage`) | Hides groups confirmed as medallion pipeline stages via `table_lineage` or catalog-tier heuristic | Heuristic can misfire; no way to review borderline cases |
# MAGIC | Hide shared-source groups | Tag-based (`shared_source`) | Hides groups where all tables share a common upstream source | Shown by default (informational), but no way to filter *only* shared-source groups |
# MAGIC | Catalog prefix — Any | String prefix match on `table.split('.')[0]` with `.some()` | Shows groups where *at least one* table's catalog matches the prefix | Combined any/all mode was only just added; still no schema-level equivalent |
# MAGIC | Catalog prefix — All | String prefix match with `.every()` | Shows groups where *all* tables' catalogs match the prefix | — |
# MAGIC
# MAGIC ### Remaining gaps
# MAGIC
# MAGIC - No filter on **similarity score range** — users cannot isolate high-confidence duplicates (>80%) from borderline ones (50–60%) without re-running detection at a different threshold.
# MAGIC - No filter on **group size** — the most actionable groups are often those with 3+ tables; pairs are high-noise at scale.
# MAGIC - No **cross-catalog-only** toggle — same-catalog duplicates are usually less urgent than those spanning catalog boundaries.
# MAGIC - No **schema prefix** filter — common need: isolate groups touching `catalog_40_copper.analytics.*`.
# MAGIC - No **table type** filter — VIEW-heavy groups are rarely actionable; TABLE-only groups are the priority.
# MAGIC - No **owner** filter — cross-owner duplicates are higher priority for clean-up conversations.
# MAGIC - No **free-text search** — no way to quickly find groups containing a known table by name.
# MAGIC - No **sort controls** — groups are returned in detection order, not prioritised by impact.
# MAGIC - No **reviewed/dismissed state** — once a user has assessed a group, there is no way to mark it and move on.

# COMMAND ----------

# DBTITLE 1,Section 2: Filtering Improvements
# MAGIC %md
# MAGIC ## 2. Filtering Improvements
# MAGIC
# MAGIC All of the following can be implemented as additions to `state.filters` and handled inside `applyGroupFilters`. None require backend changes — all necessary data is already present in each group object returned by the API.
# MAGIC
# MAGIC 1. **Minimum group size** — a numeric input or small slider (default: 2, range: 2–8+). Filters out groups where `g.tables.length < N`. At scale, pairs with a 55% composite score are extremely common and rarely actionable; setting this to 3+ immediately cuts noise dramatically.
# MAGIC
# MAGIC 2. **Cross-catalog only toggle** — a checkbox that applies `new Set(g.tables.map(t => t.split('.')[0])).size > 1`. Cross-catalog duplicates are the highest-value targets for data architects because they represent redundant ingestion or migration leftovers rather than internal pipeline stages.
# MAGIC
# MAGIC 3. **Similarity score range** — a min/max dual slider (0–100%) applied against `Math.max(...g.pairs.map(p => p.composite_score))`. Separates high-confidence duplicates (worth investigating immediately) from borderline ones (worth monitoring). More precise than the existing detection threshold re-run, and instant since it filters in-browser.
# MAGIC
# MAGIC 4. **Schema prefix filter** — mirrors the catalog prefix logic but matches against `table.split('.')[1]` (the schema component). Allows users to isolate groups touching a specific schema (e.g. `analytics`, `reporting`, `raw`) across multiple catalogs.
# MAGIC
# MAGIC 5. **Table type filter** — a multi-select toggle for `TABLE`, `VIEW`, and `EXTERNAL`. Applied as: show only groups where at least one (or all) tables match the selected type. Requires the table type to be available on the group object; if not, it can be looked up from `state.tables`.
# MAGIC
# MAGIC 6. **Owner filter** — a dropdown populated from `[...new Set(state.tables.map(t => t.owner))]`. Filters to groups where at least one table is owned by the selected principal. Cross-owner groups (owner A in one catalog, owner B in another) are typically the highest-priority for remediation conversations.
# MAGIC
# MAGIC 7. **Free-text search** — a debounced text input (reuse the existing 400ms debounce pattern from the catalog prefix input). Matches against `g.tables.some(t => t.toLowerCase().includes(query))` and also against `g.label`. Essential for finding the group containing a specific known table without scrolling through hundreds of cards.
# MAGIC
# MAGIC 8. **Sort controls** — a `<select>` with options:
# MAGIC    - Max similarity score (descending) — default
# MAGIC    - Group size (descending) — surfaces the broadest duplication
# MAGIC    - Number of distinct catalogs (descending) — surfaces cross-catalog problems first
# MAGIC    - Most recently updated table — surfaces recent duplication events
# MAGIC    
# MAGIC    Applied as a sort step after filtering, before slicing to `state.groupsShown`. Stateless: just reorders the `filtered` array before rendering.

# COMMAND ----------

# DBTITLE 1,Section 3: Group Display Improvements
# MAGIC %md
# MAGIC ## 3. Group Display Improvements
# MAGIC
# MAGIC These are changes to `renderDupGroupCard` and the surrounding page layout. Each is independent and can be shipped individually.
# MAGIC
# MAGIC 1. **Collapsible pair table** — The pairs `<table>` inside each card is currently always visible and is the noisiest part of the UI. Collapse it by default behind a `<details>`/`<summary>` element (or a toggle div with `display:none`). Show only the table chips and gold standard in the collapsed state. A group with 3 tables and a 92% score is immediately interpretable from the chips alone; the pair-level breakdown is only needed when investigating.
# MAGIC
# MAGIC 2. **Reviewed / dismissed state** — Add a small “Dismiss” button to each `renderDupGroupCard`. On click, store the group’s label (or a stable hash of its table list) in `localStorage`. In `applyGroupFilters`, exclude dismissed groups unless a “Show dismissed” checkbox is active. Dismissed groups should still appear in total counts but not in the rendered list. This gives users a persistent scratchpad without any backend changes.
# MAGIC
# MAGIC 3. **Compact view mode** — A page-level toggle (icon button in the filter bar) switches `renderDuplicates` between the current card layout and a dense table view: one row per group with columns for label, table count, max score, catalog count, gold standard (truncated), tags, and a Compare button. Renders 100+ groups on screen simultaneously. Implement by branching inside `renderDuplicates` on a `state.compactView` boolean.
# MAGIC
# MAGIC 4. **Active filter summary bar** — A sticky `<div>` rendered above `#dup-groups` that lists the currently active non-default filters as dismissible chips (e.g. `Min size: 3 ×`, `Cross-catalog only ×`, `Prefix: catalog_40 ×`). Each chip clears that individual filter. A “Clear all” resets `state.filters` to defaults. This makes it obvious at a glance why certain groups are hidden, and reduces the frustration of forgetting an active filter.
# MAGIC
# MAGIC 5. **Anchor links** — Give each group card an `id` attribute derived from a stable slug of its `g.label` (e.g. `dup-group-student-results`). The page URL updates to `#/duplicates#dup-group-student-results` on card click. Allows specific groups to be shared in Slack or bookmarked for later review.
# MAGIC
# MAGIC 6. **Lineage depth badge** — From Enhancement 4 in the Lineage Enhancement Plan: display the deepest common ancestor and pipeline hop count directly on each card for groups tagged `pipeline_stage` or `shared_source`. E.g. a badge reading `🔗 bronze.raw.pupil_census → 3 hops`. This lets users dismiss pipeline-stage groups at a glance without opening the Compare view to understand the lineage relationship.

# COMMAND ----------

# DBTITLE 1,Section 4: Schema & Dataset-Level Duplication Detection
# MAGIC %md
# MAGIC ## 4. Schema & Dataset-Level Duplication Detection
# MAGIC
# MAGIC The current detection engine operates at the table level. At 143K+ tables, this produces a large number of groups that are individually correct but collectively overwhelming. A complementary approach is to detect duplication at a **higher level of abstraction** — schemas and datasets — where the result set is orders of magnitude smaller and more actionable for architects.
# MAGIC
# MAGIC 1. **Schema fingerprinting** — Compute a canonical fingerprint for each schema: a sorted, normalised set of table names combined with a hash of their column signatures. Surface pairs of schemas with >70% fingerprint overlap. This catches “copy-paste schema proliferation” — e.g. `catalog_a.analytics` vs `catalog_b.analytics` that have grown to contain near-identical tables through repeated ingestion or environment promotion without proper lifecycle management.
# MAGIC
# MAGIC 2. **Schema duplicate groups page** — A new sub-view on the Duplicates page (tab or toggle), grouped by schema rather than table. Each card shows: schema A, schema B, % table overlap, % column overlap, and which tables are unique to each side. The result set is far smaller (thousands of schemas vs hundreds of thousands of table pairs), making it a much faster entry point for architectural review.
# MAGIC
# MAGIC 3. **Dataset identity via column fingerprint** — Two tables are likely the same dataset if their column sets are identical or near-identical regardless of name. Pre-cluster candidates by `(column_count, sorted_column_name_hash)` before running the full similarity pipeline. This is a cheap pre-filter that catches renames and migrations that the current `name_similarity` signal misses entirely, and would surface them as high-confidence duplicates even when table names diverge.
# MAGIC
# MAGIC 4. **Duplication heatmap** — A catalog × catalog matrix where each cell shows the percentage of tables in catalog A that have at least one duplicate candidate in catalog B. Gives data architects an immediate bird’s-eye view of where duplication is concentrated — e.g. a copper-to-silver migration that was only half completed — without needing to read individual group cards. Implementable as a small aggregation over the existing `state.groups` data.
# MAGIC
# MAGIC 5. **Owner × catalog duplication summary** — A table showing, for each `(owner, catalog)` pair, how many tables they own that have duplicate candidates in a different catalog. Surfaces the most impactful cleanup targets by person and by catalog boundary. Useful for generating team-level remediation tasks.
# MAGIC
# MAGIC 6. **Time-based duplication detection** — Flag table pairs where one was created within 30 days of the other and they share a high column similarity score (>0.8). This is a strong signal for an ad-hoc copy that was never cleaned up. The `created_at` and `updated_at` fields are already collected by `scanner.py` and available in `TableInfo`.
# MAGIC
# MAGIC 7. **“Dead duplicate” detection** — Within each duplicate group, identify tables where the non-gold-standard members have zero downstream consumers in `_consumer_counts`. These are the safest candidates to deprecate: they are duplicates that nothing is reading. Surface these with a “Safe to deprecate?” badge directly on the group card, and consider a dedicated filter to show only groups containing at least one dead duplicate.

# COMMAND ----------

# DBTITLE 1,Section 5: Implementation Priority
# MAGIC %md
# MAGIC ## 5. Implementation Priority
# MAGIC
# MAGIC Ranked by impact-to-effort ratio. All “Low effort” items are frontend-only changes to `app.js` with no backend or detection engine changes required.
# MAGIC
# MAGIC | # | Enhancement | Area | Effort | Impact | Notes |
# MAGIC |---|---|---|---|---|---|
# MAGIC | 1 | Compact view mode | Display | Low | High | Branch in `renderDuplicates` on `state.compactView` |
# MAGIC | 2 | Collapsible pair table | Display | Low | High | Wrap pairs `<table>` in `<details>` inside `renderDupGroupCard` |
# MAGIC | 3 | Reviewed / dismissed state | Display | Low | High | `localStorage` key per group; filter in `applyGroupFilters` |
# MAGIC | 4 | Minimum group size filter | Filtering | Low | High | One line in `applyGroupFilters`: `g.tables.length >= N` |
# MAGIC | 5 | Cross-catalog only toggle | Filtering | Low | High | `new Set(g.tables.map(t => t.split('.')[0])).size > 1` |
# MAGIC | 6 | Free-text search | Filtering | Low | Medium | Debounced; matches table names and `g.label` |
# MAGIC | 7 | Sort controls | Filtering | Low | Medium | Post-filter sort step before `slice(0, groupsShown)` |
# MAGIC | 8 | Active filter summary bar | Display | Low | Medium | Derive active filters from `state.filters` diff against defaults |
# MAGIC | 9 | Similarity score range sliders | Filtering | Medium | Medium | Dual range slider; filter on `max(g.pairs[].composite_score)` |
# MAGIC | 10 | Schema prefix + table type + owner filters | Filtering | Medium | Medium | Requires owner/type data on group object or lookup from `state.tables` |
# MAGIC | 11 | Dead duplicate detection | Detection | Medium | High | Extend `duplicates.py` gold scoring; add badge in `renderDupGroupCard` |
# MAGIC | 12 | Duplication heatmap | Visualisation | Medium | High | Aggregate over `state.groups`; render as CSS grid matrix |
# MAGIC | 13 | Owner × catalog summary | Visualisation | Medium | High | Aggregate over `state.groups` + `state.tables` ownership data |
# MAGIC | 14 | Schema fingerprinting + schema groups page | Detection | High | Very High | New scanner pass + new API endpoint + new frontend sub-view |
# MAGIC
# MAGIC **Effort definitions:** Low = frontend JS only, <1 day. Medium = backend + frontend, 1–3 days. High = new detection logic + API + frontend, 3+ days.

# COMMAND ----------

# DBTITLE 1,Section 6: Quick Wins — This Sprint
# MAGIC %md
# MAGIC ## 6. Quick Wins — ✅ Completed
# MAGIC
# MAGIC The five changes below are all frontend-only, achievable in a single session, and would collectively transform the navigability of the Duplicates page for a large estate.
# MAGIC
# MAGIC - **Compact view toggle** — Add a `state.compactView` boolean and a toggle button in the filter bar. When active, `renderDuplicates` renders groups as a single `<table>` (one row per group: label, # tables, max score, catalogs, gold standard, tags, Compare button) instead of the current card layout. This alone makes it practical to scan 200+ groups on a single screen.
# MAGIC
# MAGIC - **Collapsible pair table** — Wrap the `<table>` inside `renderDupGroupCard` in a `<details><summary>Show N pairs</summary>...</details>`. Collapsed by default. The table chips and gold standard badge carry enough information to assess most groups without opening the pairs breakdown. Eliminates the visual noise that currently makes each card so tall.
# MAGIC
# MAGIC - **Reviewed / dismissed state** — Add a “Dismiss” button to each card. On click, push the group’s stable key (`g.label + g.tables.sort().join(',')` hashed) into a `Set` stored in `localStorage`. In `applyGroupFilters`, call `dismissedGroups.has(key)` and exclude matches unless a “Show dismissed” checkbox is active. No backend required; survives page reloads.
# MAGIC
# MAGIC - **Minimum group size filter** — Add a small numeric input (`min=2`, `default=2`) to the existing filter bar. In `applyGroupFilters`, add: `if (state.filters.minGroupSize > 2 && g.tables.length < state.filters.minGroupSize) return false`. Setting this to 3 immediately removes all pairs, which tend to dominate the list and are the lowest-confidence results.
# MAGIC
# MAGIC - **Cross-catalog only toggle** — Add a checkbox to the filter bar. In `applyGroupFilters`, add: `if (state.filters.crossCatalogOnly && new Set(g.tables.map(t => t.split('.')[0])).size < 2) return false`. Cross-catalog groups are almost always the most architecturally significant and deserve their own fast path to the top of the list.

# COMMAND ----------

# DBTITLE 1,Section 7: Next Recommended Steps
# MAGIC %md
# MAGIC ## 7. Next Recommended Steps
# MAGIC
# MAGIC The five quick wins from Section 6 are now shipped. The Duplicates page is meaningfully more navigable: groups are collapsible, dismissible, filterable by size and catalog boundary, and can be scanned in a compact table view.
# MAGIC
# MAGIC The remaining work falls into three natural sprints based on effort and dependency.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### Sprint 2 — ✅ Completed
# MAGIC
# MAGIC These are still pure `app.js` changes, no backend required. Collectively they complete the filtering story started in Sprint 1.
# MAGIC
# MAGIC | # | Enhancement | Implementation note |
# MAGIC |---|---|---|
# MAGIC | 6 | **Free-text search** | Add a debounced text input to the filter bar. Match against `g.label` and `g.tables.some(t => t.includes(query))`. Reuse the existing 400ms debounce pattern from the catalog prefix input. |
# MAGIC | 7 | **Sort controls** | Add a `<select>` (options: max score, group size, catalog count, recency) after `applyGroupFilters`. Apply a sort step before `slice(0, groupsShown)`. Store selection in `state.sortBy`. |
# MAGIC | 8 | **Active filter summary bar** | Render a row of dismissible chips above `#dup-groups` for every non-default filter value in `state.filters`. Each chip resets that individual filter and calls `renderDuplicates()`. Include a “Clear all” button that resets the entire `state.filters` object to defaults. |
# MAGIC
# MAGIC **Suggested order:** Sort controls first (highest impact, simplest), then free-text search, then the filter summary bar.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### Sprint 3 — Medium-Effort Filtering and Display
# MAGIC
# MAGIC These require either additional data on the group object, a backend change, or a more involved frontend component.
# MAGIC
# MAGIC | # | Enhancement | Dependency | Implementation note |
# MAGIC |---|---|---|---|
# MAGIC | 9 | **Similarity score range** | None | Dual-handle range slider (min/max) in the filter bar. Filter on `Math.max(...g.pairs.map(p => p.composite_score))`. Consider a lightweight CSS-only dual range or a small vanilla JS implementation — no library needed at this scale. |
# MAGIC | 10 | **Schema prefix filter** | None | Mirror the catalog prefix control but match `t.split('.')[1]` (the schema segment). Can share the same Any/All mode toggle. |
# MAGIC | 11 | **Table type filter** | Group object needs `table_types` field | Add `table_types: list[str]` to the serialised group dict in `duplicates.py` (derived from `TableInfo.table_type`). Frontend: a multi-select toggle for TABLE / VIEW / EXTERNAL. |
# MAGIC | 12 | **Owner filter** | Group object needs `owners` field | Add `owners: list[str]` to the group dict. Frontend: a dropdown populated dynamically from the distinct owners across all groups in `state.groups`. |
# MAGIC | 13 | **Lineage depth badge on group cards** | Enhancement 4 in Lineage Plan (not yet started) | Surface the deepest common ancestor and hop count on cards tagged `pipeline_stage` or `shared_source`. Requires `shared_ancestors` data to be included in the cached group dict — currently only returned by the compare endpoint on demand. |
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### Sprint 4 — High-Impact Detection and Visualisation
# MAGIC
# MAGIC These address the root problem (too many groups) rather than making the existing list easier to navigate. Higher effort but highest long-term impact.
# MAGIC
# MAGIC | # | Enhancement | Effort | Why now |
# MAGIC |---|---|---|---|
# MAGIC | 14 | **“Dead duplicate” detection** | Medium | Add a `has_dead_duplicate` flag to each group during scoring in `duplicates.py`. A dead duplicate is a non-gold table with zero entries in `_consumer_counts`. Surface a “Safe to deprecate?” badge on the card and add a filter toggle. Actionable immediately: these are the groups where cleanup carries zero risk. |
# MAGIC | 15 | **Duplication heatmap** | Medium | A catalog × catalog matrix aggregated from `state.groups` — no new API needed. Add as a new “Heatmap” tab on the Duplicates page. Renders the full cross-catalog duplication picture in a single glance. |
# MAGIC | 16 | **Owner × catalog summary table** | Medium | A pivot of `state.groups` × `state.tables` ownership data. Surfaces which owners have the most cross-catalog duplication. Useful for generating team-level remediation tasks. |
# MAGIC | 17 | **Schema fingerprinting + schema duplicate groups** | High | New pass in `scanner.py` computing per-schema column fingerprints. New `/api/duplicates/schema-groups` endpoint. New sub-view on the Duplicates page. Result set is orders of magnitude smaller than table-level groups, making it the best long-term entry point for architectural review. |
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### Dependency summary
# MAGIC
# MAGIC ```
# MAGIC Sprint 1 (done) ─┬─ Sprint 2 (low effort, no deps)
# MAGIC                  └─ Sprint 3 (medium, some backend deps)
# MAGIC                       └─ Sprint 4 ─┬─ Dead duplicate detection (standalone)
# MAGIC                                    ├─ Heatmap (needs groups in state — already available)
# MAGIC                                    └─ Schema fingerprinting (independent, highest effort)
# MAGIC ```
# MAGIC
# MAGIC Sprints 2 and 3 can be worked in parallel on separate branches. Sprint 4 items are independent of each other and can be prioritised individually.
