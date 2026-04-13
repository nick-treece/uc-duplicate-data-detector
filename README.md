# UC Data Quality Duplicates

A Databricks App that scans Unity Catalog metadata across all accessible catalogs to find duplicate datasets, recommends gold-standard tables, and surfaces group-level access permissions — helping data architects and engineers clean up data sprawl before it reaches analysts.

## Features

| Feature | Description |
|---|---|
| **Multi-Catalog Scanner** | Background scan of every accessible catalog with live progress polling — schemas, tables, columns, types, comments, timestamps, and permissions. Per-catalog breakdowns shown on the dashboard. |
| **Result Caching** | Scan results and duplicate groups are cached in `catalog_40_copper_uc_metadata.cache`. On startup the app loads from cache instantly; a fresh scan is triggered automatically if the cache is older than 7 days or the cache schema version has changed. |
| **Permissions Viewer** | Shows which groups and users have READ / WRITE access to each table via metadata snapshot tables — no `MANAGE` privilege needed. Permissions are merged across catalog, schema, and table grant levels. |
| **Duplicate Detection** | Clusters tables that represent the same entity using column-name Jaccard similarity, type compatibility, and fuzzy table-name matching. Uses a normalised-name grouping pre-filter for scalable comparison (~100K candidates from 143K tables). Groups are labelled by common entity name. |
| **Gold Standard Scoring** | Ranks each table in a duplicate group on column completeness and freshness to recommend the canonical dataset. |
| **Table Comparison** | Side-by-side column diff, permissions diff, and sample data for any two tables. |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Databricks App                          │
│                                                              │
│  ┌──────────────┐       ┌──────────────────────────────────┐ │
│  │   Frontend    │       │       FastAPI Backend             │ │
│  │  (Vanilla JS) │◄────►│                                  │ │
│  │              │       │  /api/catalog/*   (scan, cache)   │ │
│  │  Dashboard   │       │  /api/duplicates/* (groups)       │ │
│  │  Catalog     │       │  /api/compare/*   (diff, sample)  │ │
│  │  Duplicates  │       │                                  │ │
│  │  Compare     │       │  scanner.py   (SQL Statement API) │ │
│  └──────────────┘       │  cache.py     (UC cache layer)   │ │
│                         │  duplicates.py (detection engine) │ │
│                         │  comparator.py (table diff)      │ │
│                         └────────┬─────────────────────────┘ │
└──────────────────────────────────┼───────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │       Unity Catalog               │
                    │                                   │
                    │  catalog_40_copper_uc_metadata     │
                    │   ├── metadata.*  (weekly CTAS     │
                    │   │   snapshots of                 │
                    │   │   system.information_schema)   │
                    │   └── cache.*    (scan results     │
                    │       + duplicate groups)          │
                    │                                   │
                    │  SQL Statement API                 │
                    │   (EXTERNAL_LINKS disposition)     │
                    └───────────────────────────────────┘
```

## Project structure

```
uc-duplicate-data-detector/
├── databricks.yml              # DAB bundle config (targets, variables)
├── app.yaml                    # App runtime config (command, env vars)
├── app.py                      # FastAPI entrypoint
├── requirements.txt            # Python dependencies
├── server/
│   ├── config.py               # Dual-mode auth (local CLI / deployed App)
│   ├── scanner.py              # UC metadata scanner + permissions (via SQL)
│   ├── cache.py                # Cache manager (read/write scan results to UC)
│   ├── duplicates.py           # Duplicate detection + gold standard scoring
│   ├── comparator.py           # Table comparison + sample data
│   └── routes/
│       ├── catalog.py          # /api/catalog/*  (list, scan, cache, schemas, tables)
│       ├── duplicates.py       # /api/duplicates/*
│       └── compare.py          # /api/compare/*
├── frontend/
│   └── dist/                   # Static SPA (HTML/CSS/JS, no build step)
└── scripts/
    ├── deploy.sh                      # One-command deploy (bundle + app source)
    ├── create_governance_tables.sql   # CTAS snapshots of system.information_schema (run weekly)
    ├── generate_data.py               # Test data generator (Python + CLI)
    └── generate_data.sql              # Test data generator (pure SQL)
```

## How it works

### Metadata source

All metadata is read from **snapshot tables** in `catalog_40_copper_uc_metadata.metadata` — weekly CTAS copies of `system.information_schema` tables (catalogs, schemata, tables, columns, catalog_privileges, schema_privileges, table_privileges). These snapshots provide definer-rights access so the app's service principal can read metadata across all catalogs without `MANAGE` privileges.

### Scan flow

1. **POST /api/catalog/scan-all** launches a background thread that queries each catalog sequentially (schemas, tables, columns, and three privilege levels).
2. The frontend polls **GET /api/catalog/scan-status** every 2 seconds with progress updates ("Scanning catalog_name (3/7)…").
3. After the scan, **duplicate detection** runs in the same background thread using a normalised-name pre-filter and composite similarity scoring.
4. Results are written to the **UC cache** (`catalog_40_copper_uc_metadata.cache`) so the next app restart loads instantly.
5. The SQL Statement API uses `EXTERNAL_LINKS` disposition to handle result sets exceeding the 25 MB inline limit.

### Cache layer

Scan results and duplicate groups are persisted in two Delta tables:

| Table | Contents |
|---|---|
| `cache.cache_metadata` | Single row: cache version, timestamp, serialised scan-result JSON |
| `cache.duplicate_groups` | One row per group: label, tables, pairs, gold standard, scores (complex fields as JSON) |

**Invalidation triggers:**
- Cache older than 7 days (matching the weekly metadata refresh cadence)
- `CACHE_VERSION` constant in `cache.py` differs from the stored version (covers schema changes during development)

On startup the frontend calls `GET /api/catalog/cache-status`. If valid, `POST /api/catalog/cache-load` bulk-loads tables and schemas from the metadata snapshot (2 queries total, vs 6 × N catalogs for a full scan) and restores duplicate groups from cache. Table columns and permissions are loaded lazily on demand when viewing individual table details.

## Prerequisites

- [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html) >= 0.230
- Python 3.10+
- A Databricks workspace with:
  - Unity Catalog enabled
  - A SQL warehouse (Serverless or Pro)
  - The `catalog_40_copper_uc_metadata` catalog with a `metadata` schema (weekly CTAS snapshots)
  - Permission to create Apps

## Permissions

The app's service principal needs **no `MANAGE` privilege**. Permissions are read from weekly CTAS snapshot tables in `catalog_40_copper_uc_metadata.metadata` — mirrors of `system.information_schema` that provide definer-rights access.

### Required grants for the service principal

| Privilege | Scope | Purpose |
|---|---|---|
| `USE CATALOG` | `catalog_40_copper_uc_metadata` | Access the app catalog |
| `USE SCHEMA` | `catalog_40_copper_uc_metadata.metadata` | Read metadata snapshots |
| `SELECT` | `catalog_40_copper_uc_metadata.metadata` | Query schemas, tables, columns, privileges |
| `CREATE SCHEMA` | `catalog_40_copper_uc_metadata` | Create the `cache` schema on first scan |
| `CAN_USE` | SQL warehouse | Execute SQL queries |

### Quick setup

**Step 1 — Create metadata snapshot tables** (schedule weekly):

Open `scripts/create_governance_tables.sql` in a Databricks SQL editor and run all statements. This creates CTAS snapshot tables in `catalog_40_copper_uc_metadata.metadata` that mirror `system.information_schema` views. The executing user becomes the schema owner (definer) — their privileges determine what metadata is visible at refresh time.

**Step 2 — Grant the SP access** (replace `<SP_ID>`):

```sql
-- Metadata snapshot access
GRANT USE CATALOG ON CATALOG catalog_40_copper_uc_metadata TO `<SP_ID>`;
GRANT USE SCHEMA ON SCHEMA catalog_40_copper_uc_metadata.metadata TO `<SP_ID>`;
GRANT SELECT ON SCHEMA catalog_40_copper_uc_metadata.metadata TO `<SP_ID>`;

-- Cache schema (created automatically, but SP needs permission to create it)
GRANT CREATE SCHEMA ON CATALOG catalog_40_copper_uc_metadata TO `<SP_ID>`;
```

## Getting started

### 1. Clone and authenticate

```bash
git clone https://github.com/richardli29/uc-data-quality-duplicates.git && cd uc-data-quality-duplicates

databricks auth login --host https://<WORKSPACE>.cloud.databricks.com
```

### 2. Find your warehouse ID

```bash
databricks warehouses list --output json | python3 -c "
import json, sys
for w in json.load(sys.stdin):
    print(f'{w[\"id\"]}  {w[\"name\"]}  ({w[\"state\"]})')
"
```

### 3. Configure

Edit **two files** with your workspace details:

**`app.yaml`** — set the SQL warehouse for runtime queries:

```yaml
env:
  - name: WAREHOUSE_ID
    value: "abc123def456"
```

**`databricks.yml`** — set the workspace and warehouse for deployment:

```yaml
targets:
  dev:
    workspace:
      host: https://<WORKSPACE>.cloud.databricks.com
    variables:
      warehouse_id: abc123def456
```

> **Note:** `CATALOG_NAME` is not required. The app discovers and scans all accessible catalogs automatically.

### 4. (Optional) Generate test data

Creates 20 education-themed tables across 5 schemas with deliberate duplicates.

**Option A — SQL notebook** (paste into a Databricks SQL editor):

```bash
# Open scripts/generate_data.sql and set the widget to your catalog name
```

**Option B — Python CLI**:

```bash
python3 scripts/generate_data.py \
  --catalog my_catalog \
  --warehouse abc123def456
```

| Schema | Tables | Purpose |
|---|---|---|
| `bronze` | `raw_students`, `raw_schools`, `raw_exam_results`, `raw_attendance` | Raw ingestion layer with original column names |
| `silver` | `students`, `schools`, `exam_results`, `attendance` | Cleaned and standardised |
| `gold` | `dim_students`, `dim_schools`, `fact_exam_results`, `fact_attendance_agg` | Curated with SCD tracking, aggregations, and documentation |
| `team_analytics` | `student_data`, `school_info`, `exam_scores`, `student_attendance` | Duplicate set with renamed columns |
| `team_reporting` | `pupils`, `school_directory`, `assessment_results`, `attendance_register` | Another duplicate set with different naming |

### 5. Deploy

```bash
./scripts/deploy.sh
```

Or step by step:

```bash
databricks bundle validate
databricks bundle deploy
databricks apps start uc-data-duplicates
databricks apps deploy uc-data-duplicates \
  --source-code-path /Workspace/Users/<you>/.bundle/uc-data-duplicates/dev/files
```

### 6. Grant permissions to the app service principal

Find the SP ID:

```bash
databricks apps get uc-data-duplicates --output json | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['service_principal_client_id'])
"
```

Then grant access (replace `<SP_ID>`):

```sql
-- Metadata snapshots
GRANT USE CATALOG ON CATALOG catalog_40_copper_uc_metadata TO `<SP_ID>`;
GRANT USE SCHEMA ON SCHEMA catalog_40_copper_uc_metadata.metadata TO `<SP_ID>`;
GRANT SELECT ON SCHEMA catalog_40_copper_uc_metadata.metadata TO `<SP_ID>`;

-- Cache schema creation
GRANT CREATE SCHEMA ON CATALOG catalog_40_copper_uc_metadata TO `<SP_ID>`;
```

And grant `CAN_USE` on the SQL warehouse:

```bash
databricks api patch /api/2.0/permissions/sql/warehouses/<WAREHOUSE_ID> \
  --json '{"access_control_list":[{"service_principal_name":"<SP_ID>","permission_level":"CAN_USE"}]}'
```

### 7. Open the app

```bash
databricks apps get uc-data-duplicates
```

Open the `url` from the output in your browser. You must be logged into the workspace first.

## Using the app

1. **Dashboard** — on first load, the app checks for a valid cache. If found, results load instantly with a green "Loaded from cache" banner. Otherwise, click **Scan All Catalogs** to start a background scan with live progress. Stat cards show total schemas, tables, columns, duplicate groups, and per-catalog breakdowns.
2. **Catalog Explorer** — browse all catalogs in a tree (catalog > schema > table). Click a table to see columns, owner, comments, and merged permissions (loaded on demand).
3. **Duplicates** — view duplicate clusters labelled by entity name (e.g. "Students", "Exam Results") with similarity scores and gold-standard badges. Adjust the threshold slider and re-detect. Cross-catalog duplicates are detected too.
4. **Compare** — pick any two tables (from any catalog) for a side-by-side column diff, permissions comparison, and sample data preview.

## API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/catalog/list` | List all accessible catalogs |
| `POST` | `/api/catalog/scan-all` | Start a background scan (returns immediately) |
| `GET` | `/api/catalog/scan-status` | Poll current scan progress |
| `GET` | `/api/catalog/cache-status` | Check whether a valid cache exists |
| `POST` | `/api/catalog/cache-load` | Load scan results from cache + bulk metadata |
| `GET` | `/api/catalog/schemas?catalog=X` | List scanned schemas (optional catalog filter) |
| `GET` | `/api/catalog/tables?schema=gold&catalog=X` | List tables (lightweight summaries, no columns) |
| `GET` | `/api/catalog/table/{catalog}/{schema}/{table}` | Full metadata for one table (columns + permissions loaded on demand) |
| `GET` | `/api/duplicates/detect?threshold=0.5` | Force re-detection with a custom threshold |
| `GET` | `/api/duplicates/groups` | Return pre-computed duplicate groups |
| `GET` | `/api/compare/{cat1}/{s1}/{t1}/{cat2}/{s2}/{t2}` | Column + permissions diff between two tables |
| `GET` | `/api/compare/sample/{catalog}/{schema}/{table}` | Fetch 10 sample rows from a table |

## Customisation

### Similarity weights

Edit `server/duplicates.py` — the `detect_duplicates` function accepts:

- `col_weight` (default 0.50) — Jaccard index on canonical column names
- `type_weight` (default 0.30) — proportion of shared columns with compatible types
- `name_weight` (default 0.20) — token-based Jaccard on table names

### Gold standard scoring

Each table in a duplicate group is scored on:

- **Column completeness** (10 pts) — tables with more columns score higher
- **Freshness** (10 pts) — recently updated tables score higher

### Synonym mappings

Edit the `_SYNONYMS` dictionary in `server/duplicates.py` to add domain-specific column name mappings (e.g. `student_id` = `learner_id` = `pupil_id`). This improves duplicate detection when teams use different naming conventions.

### Cache configuration

Edit `server/cache.py` to change:

- `CACHE_MAX_AGE_DAYS` (default 7) — how long before the cache expires
- `CACHE_VERSION` (default "1") — bump this when changing cache table schemas to force a rebuild

## License

MIT
