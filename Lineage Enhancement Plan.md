# Lineage Enhancement Plan

**Date:** 2026-04-27
**Context:** UC Data Duplicate Detector (`uc-data-duplicates` app)
**Lineage source:** `catalog_40_copper_uc_metadata.metadata.table_lineage` (90-day window)

---

## The Problem: Single-Hop Ancestry Misses Deep Relationships

The current `_build_lineage_lookups()` builds **single-hop** upstream/downstream maps. `shared_upstream` only finds tables that share an immediate parent. Analysis of the live lineage graph reveals significant depth that is being ignored:

| Metric | Value |
|---|---|
| Total unique nodes | 132,136 |
| Total distinct edges | 202,301 |
| Average edges per node | 1.5 |
| Tables with direct parents (depth 1) | 101,333 |
| Tables with grandparents (depth 2) | 71,538 |
| Tables with great-grandparents (depth 3) | 51,127 |
| Median fan-in per table | 1 |
| P95 fan-in | 5 |
| P99 fan-in | 6 |
| Max fan-in (hub tables) | 3,797 |

**71K tables have grandparents and 51K have great-grandparents** — meaning the current single-hop `shared_upstream` is missing the majority of common ancestor relationships. Two tables that both trace back to the same source table through different intermediate pipeline stages (e.g. bronze → silver\_a → gold\_a and bronze → silver\_b → gold\_b) will not be recognised as sharing a common ancestor.

---

## Enhancement 1: Deep Common Ancestor Detection ✅

**Status:** Deployed 2026-04-27 | **Test plan:** `TEST_PLAN_ENHANCEMENT_1.md`

**Approach:** Depth-capped BFS on the in-memory upstream graph.

After `_build_lineage_lookups()` completes, run a second pass that computes **transitive ancestors** for every table using breadth-first search up to a configurable depth cap (recommended: 5 hops). The graph is sparse enough (median fan-in of 1, P99 of 6) that BFS is cheap for the vast majority of tables.

**Backend changes (`scanner.py`):**

```
_transitive_upstream: dict[str, dict[str, int]]
# Maps table → {ancestor: min_depth}
```

- New method `_build_transitive_upstream(max_depth=5, max_ancestors=500)`
- BFS from each table, walking `upstream_map` edges
- Depth cap of 5 prevents runaway traversal
- Ancestor cap of 500 per table handles hub tables (the 193 tables with >20 parents)
- Store as `{ancestor_table: shortest_path_depth}` for each table
- Called once at scan/cache-load time after `_build_lineage_lookups()`

**Detection engine changes (`duplicates.py`):**

- `lineage_similarity()` currently checks single-hop shared upstream via Jaccard
- Enhanced version: intersect transitive ancestor sets, weighted by depth (closer ancestors score higher)
- Scoring formula: `sum(1/depth for each shared ancestor) / max_possible` — rewards shared parents more than shared great-great-grandparents

**Compare page changes (`comparator.py`):**

- `build_lineage_context()` returns a new `shared_ancestors` field:
  ```
  shared_ancestors: [
    {name: "bronze.raw.source_table", depth_a: 2, depth_b: 3},
    {name: "landing.ingest.raw_feed", depth_a: 3, depth_b: 3},
  ]
  ```
- Frontend renders these with depth badges, sorted by minimum combined depth (closest common ancestor first)

**Actual parameters (tuned for memory):** `max_depth=3`, `max_ancestors=200` (~75MB)

---

## Enhancement 2: Lineage-Based Candidate Discovery ✅

**Status:** Deployed 2026-04-28 | **Test plan:** `TEST_PLAN_ENHANCEMENT_2.md`

Currently, `_build_candidate_pairs()` only groups tables by name tokens. Tables with different names but the same upstream source are never compared.

**Approach:** Add a second candidate-generation pass that pairs tables sharing deep ancestors.

- After transitive upstream is computed, group tables by their top-N closest ancestors
- If two tables (in different schemas/catalogs) share >50% of their top-5 ancestors, add them as candidates regardless of name similarity
- This catches the "same source, different name" pattern that the name-only pre-filter misses

**Guard rails:**
- Only consider ancestors at depth ≤ 3 to avoid pairing every table in the estate through a single hub source
- Skip ancestors that appear in >100 tables' transitive upstream (they are too generic to be meaningful — e.g. a raw landing table that feeds everything)

**Results:** 49,952 new candidate pairs discovered (3.6x increase over name-only). Generation time: 12.9s.

---

## Enhancement 3: Interactive Lineage Visualisation ✅

**Status:** Deployed

The Compare page now renders the lineage subgraph connecting two compared tables — showing how they relate through shared ancestors, direct flows, and intermediate pipeline stages.

**Technology:** D3.js + dagre (loaded from CDN in `index.html`). No build step required.

**What was implemented:**

1. **API endpoint** (`GET /api/compare/lineage-graph/{cat1}/{s1}/{t1}/{cat2}/{s2}/{t2}`) — returns `{nodes, edges}` for the connecting subgraph. BFS upstream from both tables, prunes branches that don't connect through shared ancestors, caps at 100 nodes.

2. **Frontend renderer** (`renderDagreGraph` in `app.js`) — D3 + dagre layout with:
   - Left-to-right DAG layout
   - Node colouring: blue = compared table, purple = shared ancestor, dark = intermediate
   - Tier indicator dots (gold/silver/bronze/copper) on each node
   - Zoom/pan via D3 zoom
   - Hover tooltip: full table name, tier, consumers, depth from each target
   - Collapsible section ("Lineage Graph") — graph loads on-demand when expanded

---

## Enhancement 4: Lineage Depth in Duplicate Group Cards ✅

**Status:** Deployed

Duplicate group cards now show:

- **Common source** — `schema.table` of the closest ancestor shared by all tables in the group, with the hop range (e.g. "2–3 hops")
- **Lineage coverage** — percentage of group members that have transitive upstream data, colour-coded green/yellow/grey

Implemented via `_compute_group_lineage_info()` in `duplicates.py`, called in Phase 5b of `detect_duplicates()`. The `lineage_info` dict is serialised into each group's cached dict.

---

## Implementation Order

| Step | Enhancement | Effort | Dependencies | Status |
|---|---|---|---|---|
| 1 | Deep common ancestor detection (BFS) | 1-2 days | None — pure backend | ✅ Deployed |
| 2 | Enhanced lineage scoring in detection | 1 day | Step 1 | ✅ Deployed (part of E1) |
| 3 | Shared ancestors on compare page | 0.5 day | Step 1 | ✅ Deployed (part of E1) |
| 4 | Lineage-based candidate discovery | 1 day | Step 1 | ✅ Deployed |
| 5 | Lineage depth in group cards | 1 day | Steps 1-2 | ✅ Deployed |
| 6 | Lineage graph API endpoint | 2-3 days | Step 1 | ✅ Deployed |
| 7 | D3-dagre visualisation | 3-4 days | Step 6 | ✅ Deployed |

**All 7 enhancements are complete.**
