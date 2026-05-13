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

## Enhancement 3: Interactive Lineage Visualisation

**Goal:** Render the lineage subgraph connecting two compared tables on the Compare page, showing how they relate through shared ancestors, direct flows, and intermediate pipeline stages.

**Technology choice: D3.js + dagre (DAG layout)**

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Mermaid.js** | Simplest integration (text → SVG), no code | No interactivity, no click-to-expand, fixed layout | MVP only |
| **D3.js + dagre** | Full control over layout, interactive (hover, click, zoom), best for DAGs | More code, CDN dependency | **Recommended** |
| **Elkjs** | Best automatic layout quality | Heavy library (\~200KB), complex API | Over-engineered |
| **Vis.js Network** | Easy force-directed graphs | Not ideal for directed acyclic graphs (lineage is a DAG) | Poor fit |

**Implementation plan:**

1. **New API endpoint:** `GET /api/compare/lineage-graph/{cat1}/{s1}/{t1}/{cat2}/{s2}/{t2}`
   - Returns a JSON graph: `{nodes: [{id, label, tier, is_target}], edges: [{source, target, entity_types}]}`
   - Computes the **connecting subgraph**: start from both tables, walk upstream (BFS, max depth 5), collect all nodes and edges on paths that connect them through shared ancestors
   - Prune disconnected branches (nodes that don't lead to a shared ancestor)
   - Cap at 100 nodes to keep the visualisation readable

2. **Frontend: D3-dagre renderer**
   - Load `d3` and `dagre-d3` from CDN (no build step needed, \~60KB total)
   - Left-to-right DAG layout (sources on left, targets on right)
   - Node colouring by medallion tier (bronze/silver/gold) or catalog
   - The two compared tables highlighted with accent border
   - Shared ancestors highlighted with a distinct colour
   - Edges labelled with entity types (JOB, NOTEBOOK, DBSQL_QUERY)
   - Hover: show full table name, consumer count, depth from each target
   - Click: navigate to that table's detail page in the Catalog Explorer

3. **Frontend: placement on Compare page**
   - New expandable section "Lineage Graph" below the existing lineage lists
   - Collapsed by default (graph is loaded on-demand when expanded to avoid unnecessary API calls)
   - Zoomable/pannable SVG container with a reset-zoom button

**Estimated effort:** 2-3 days backend (subgraph extraction), 3-4 days frontend (D3-dagre renderer + interactivity).

---

## Enhancement 4: Lineage Depth in Duplicate Group Cards

Currently, duplicate group cards on the Duplicates page show tags (`pipeline_stage`, `shared_source`) but no lineage detail. With transitive upstream available, each group card could show:

- **Deepest common ancestor** — the table that is the root source for all members of the group
- **Pipeline depth** — number of hops from the common ancestor to each member
- **Lineage confidence** — percentage of group members that have any lineage data at all

This gives users immediate context about *why* tables are grouped without needing to open the compare page.

**Estimated effort:** 0.5 day backend, 0.5 day frontend.

---

## Implementation Order

| Step | Enhancement | Effort | Dependencies | Status |
|---|---|---|---|---|
| 1 | Deep common ancestor detection (BFS) | 1-2 days | None — pure backend | ✅ Deployed |
| 2 | Enhanced lineage scoring in detection | 1 day | Step 1 | ✅ Deployed (part of E1) |
| 3 | Shared ancestors on compare page | 0.5 day | Step 1 | ✅ Deployed (part of E1) |
| 4 | Lineage-based candidate discovery | 1 day | Step 1 | ✅ Deployed |
| 5 | Lineage depth in group cards | 1 day | Steps 1-2 | Not started |
| 6 | Lineage graph API endpoint | 2-3 days | Step 1 | Not started |
| 7 | D3-dagre visualisation | 3-4 days | Step 6 | Not started |

Steps 1-4 are deployed and delivering analytical value. Steps 5 adds lineage context to group cards. Steps 6-7 (the visualisation) are the most effort but also the most impactful for user understanding.
