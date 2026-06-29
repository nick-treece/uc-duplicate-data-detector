"""Cache manager — stores scan results in Unity Catalog tables.

Cache location: ``catalog_40_copper_uc_metadata.cache``

Two tables are maintained:

* ``cache_metadata``  — one row per scan with version, timestamp, and the
  serialised scan-result summary.  Each scan appends a new row with an
  auto-incremented ``scan_id``, preserving history.
* ``duplicate_groups`` — one row per duplicate group per scan, keyed by
  ``scan_id``.  Complex fields (tables, pairs, gold_scores, tags) are
  stored as JSON strings.

The app always loads the **latest** ``scan_id``.  Previous scans remain
in the tables as an auditable version history.

Invalidation rules
------------------
1. Latest cache older than ``CACHE_MAX_AGE_DAYS`` (7 days).
2. Automatic fingerprint mismatch — ``CACHE_VERSION`` is computed at
   import time from the cache contract (dataclass fields, DDL columns,
   detection algorithm defaults).  Any structural or algorithmic change
   invalidates stale caches without manual version bumps.
"""

from __future__ import annotations

import hashlib
import re
import inspect
import json
import logging
from dataclasses import fields as dc_fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.scanner import CatalogScanner

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────
CACHE_SCHEMA = "catalog_40_copper_uc_metadata.cache"
CACHE_MAX_AGE_DAYS = 7

# Cache table DDL — single source of truth for both ensure_schema()
# and the fingerprint.  Column names are derived automatically.
_TABLE_DDLS = {
    "cache_metadata": """
        scan_id          INT,
        cache_version    STRING,
        cached_at        TIMESTAMP,
        scan_result_json STRING
    """,
    "duplicate_groups": """
        scan_id          INT,
        group_id         INT,
        label            STRING,
        tables_json      STRING,
        pairs_json       STRING,
        gold_standard    STRING,
        gold_scores_json STRING,
        tags_json        STRING
    """,
    "schema_groups": """
        scan_id     INT,
        result_json STRING
    """,
}

_COL_PATTERN = re.compile(r"(\w+)\s+(?:INT|STRING|TIMESTAMP)\b")
_DDL_COLUMNS = {
    table: _COL_PATTERN.findall(ddl) for table, ddl in _TABLE_DDLS.items()
}


def _compute_cache_fingerprint() -> str:
    """Derive a deterministic version string from the cache contract.

    Inputs hashed:
      1. DuplicateGroup / DuplicatePair field names  (what gets serialised)
      2. Cache table DDL column names                 (what gets stored)
      3. detect_duplicates numeric defaults            (algorithm parameters)

    Any structural or algorithmic change produces a new fingerprint,
    automatically invalidating stale caches without manual version bumps.
    """
    from server.duplicates import DuplicateGroup, DuplicatePair, detect_duplicates

    group_fields = [f.name for f in dc_fields(DuplicateGroup)]
    pair_fields = [f.name for f in dc_fields(DuplicatePair)]

    sig = inspect.signature(detect_duplicates)
    algo_defaults = {
        name: param.default
        for name, param in sig.parameters.items()
        if param.default is not inspect.Parameter.empty
        and isinstance(param.default, (int, float))
    }

    contract = {
        "group_fields": group_fields,
        "pair_fields": pair_fields,
        "ddl_columns": _DDL_COLUMNS,
        "algo_defaults": algo_defaults,
    }

    contract_json = json.dumps(contract, sort_keys=True)
    return hashlib.sha256(contract_json.encode()).hexdigest()[:12]


CACHE_VERSION = _compute_cache_fingerprint()


class CacheManager:
    """Read / write processed scan results in Unity Catalog tables."""

    def __init__(self, scanner: "CatalogScanner"):
        self._scanner = scanner

    # ── Private helpers ───────────────────────────────────────────────────

    def _run_sql(self, sql: str, quiet: bool = False):
        """Delegate SQL execution to the scanner."""
        return self._scanner._run_sql(sql, quiet=quiet)

    @staticmethod
    def _esc(value: str | None) -> str:
        """Escape a value for inclusion in a SQL single-quoted string."""
        if value is None:
            return ""
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    # ── Schema & table creation ───────────────────────────────────────────

    def ensure_schema(self):
        """Create the cache schema and tables if they don't already exist."""
        self._run_sql(f"CREATE SCHEMA IF NOT EXISTS {CACHE_SCHEMA}")

        for table_name, col_ddl in _TABLE_DDLS.items():
            self._run_sql(
                f"CREATE TABLE IF NOT EXISTS "
                f"{CACHE_SCHEMA}.{table_name} ({col_ddl})"
            )

    # ── Scan ID management ────────────────────────────────────────────────

    def _next_scan_id(self) -> int:
        """Return the next scan_id (MAX + 1, or 1 if table is empty)."""
        rows = self._run_sql(
            f"SELECT COALESCE(MAX(scan_id), 0) "
            f"FROM {CACHE_SCHEMA}.cache_metadata",
            quiet=True,
        )
        current_max = int(rows[0][0]) if rows and rows[0][0] is not None else 0
        return current_max + 1

    def _latest_scan_id(self) -> int | None:
        """Return the scan_id of the most recent scan, or None."""
        rows = self._run_sql(
            f"SELECT MAX(scan_id) FROM {CACHE_SCHEMA}.cache_metadata",
            quiet=True,
        )
        if not rows or rows[0][0] is None:
            return None
        return int(rows[0][0])

    # ── Cache validity ────────────────────────────────────────────────────

    def get_cache_status(self) -> dict:
        """Return whether the cache is valid and why / why not.

        Keys: ``valid``, ``reason``, ``cached_at``, ``age_days``,
        ``scan_id``.
        """
        try:
            rows = self._run_sql(
                f"SELECT scan_id, cache_version, cached_at, "
                f"       datediff(current_timestamp(), cached_at) AS age_days "
                f"FROM {CACHE_SCHEMA}.cache_metadata "
                f"ORDER BY scan_id DESC LIMIT 1",
                quiet=True,
            )
        except Exception:
            return {"valid": False, "reason": "Cache tables do not exist yet"}

        if not rows:
            return {"valid": False, "reason": "No cache found"}

        scan_id = int(rows[0][0])
        version = rows[0][1]
        cached_at = rows[0][2]
        age_days = float(rows[0][3]) if rows[0][3] is not None else 999

        if version != CACHE_VERSION:
            return {
                "valid": False,
                "reason": (
                    f"Cache version mismatch "
                    f"(stored={version}, expected={CACHE_VERSION})"
                ),
                "cached_at": cached_at,
                "age_days": age_days,
                "scan_id": scan_id,
            }

        if age_days > CACHE_MAX_AGE_DAYS:
            return {
                "valid": False,
                "reason": (
                    f"Cache is {age_days:.0f} days old "
                    f"(max {CACHE_MAX_AGE_DAYS})"
                ),
                "cached_at": cached_at,
                "age_days": age_days,
                "scan_id": scan_id,
            }

        return {
            "valid": True,
            "reason": "Cache is valid",
            "cached_at": cached_at,
            "age_days": age_days,
            "scan_id": scan_id,
        }

    # ── Write cache ───────────────────────────────────────────────────────

    def write_cache(self, scan_result: dict, groups: list[dict], schema_groups: list[dict] | None = None):
        """Persist scan results and duplicate groups to the cache tables.

        Each call appends a new ``scan_id`` — previous scans are retained
        as version history.
        """
        logger.info("Writing scan results to cache \u2026")

        self.ensure_schema()

        scan_id = self._next_scan_id()
        logger.info(f"Assigning scan_id={scan_id}")

        # ── Metadata row ──────────────────────────────────────────────────
        scan_json = self._esc(json.dumps(scan_result, default=str))

        self._run_sql(
            f"INSERT INTO {CACHE_SCHEMA}.cache_metadata VALUES "
            f"({scan_id}, '{CACHE_VERSION}', current_timestamp(), "
            f"'{scan_json}')"
        )

        # ── Duplicate groups ──────────────────────────────────────────────
        if groups:
            self._write_groups_batched(scan_id, groups)

        # ── Schema groups ─────────────────────────────────────────────
        if schema_groups:
            self._write_schema_groups_batched(scan_id, schema_groups)

        logger.info(
            f"Cache written \u2014 scan_id={scan_id}, "
            f"{len(groups)} duplicate group(s), "
            f"{len(schema_groups or [])} schema group(s)"
        )

    # SQL Statement API has a ~1MB payload limit; stay well under it.
    _MAX_SQL_BYTES = 800_000

    def _write_groups_batched(self, scan_id: int, groups: list[dict]):
        """INSERT groups with adaptive batching based on SQL payload size.

        Instead of a fixed batch_size (which fails when individual groups
        contain large pairs_json), this accumulates rows until the SQL
        statement approaches the API payload limit, then flushes.
        """
        insert_prefix = f"INSERT INTO {CACHE_SCHEMA}.duplicate_groups VALUES "
        prefix_len = len(insert_prefix)

        value_rows: list[str] = []
        current_len = prefix_len

        for g in groups:
            row = self._serialise_group_row(scan_id, g)
            row_len = len(row) + 2  # account for ", " separator

            # Flush if adding this row would exceed the limit
            if value_rows and (current_len + row_len) > self._MAX_SQL_BYTES:
                self._flush_group_rows(insert_prefix, value_rows)
                value_rows = []
                current_len = prefix_len

            value_rows.append(row)
            current_len += row_len

        # Flush remaining
        if value_rows:
            self._flush_group_rows(insert_prefix, value_rows)

    def _serialise_group_row(self, scan_id: int, g: dict) -> str:
        """Serialise a single group dict into a SQL VALUES tuple string."""
        gid = int(g["group_id"])
        label = self._esc(g.get("label", ""))
        tables = self._esc(json.dumps(g.get("tables", [])))
        pairs = self._esc(json.dumps(g.get("pairs", []), default=str))
        gold = self._esc(g.get("gold_standard") or "")
        scores = self._esc(json.dumps(g.get("gold_scores", {}), default=str))
        tags = self._esc(json.dumps(g.get("tags", [])))
        return (
            f"({scan_id}, {gid}, '{label}', '{tables}', "
            f"'{pairs}', '{gold}', '{scores}', '{tags}')"
        )

    def _flush_group_rows(self, prefix: str, rows: list[str]):
        """Execute one INSERT statement with the accumulated rows."""
        sql = prefix + ", ".join(rows)
        logger.debug(f"Cache INSERT: {len(rows)} rows, {len(sql):,} bytes")
        self._run_sql(sql)

    def _serialise_schema_row(self, scan_id: int, g: dict) -> str:
        result = self._esc(json.dumps(g, default=str))
        return f"({scan_id}, '{result}')"

    def _write_schema_groups_batched(self, scan_id: int, schema_groups: list[dict]):
        insert_prefix = f"INSERT INTO {CACHE_SCHEMA}.schema_groups VALUES "
        prefix_len = len(insert_prefix)
        value_rows: list[str] = []
        current_len = prefix_len
        for g in schema_groups:
            row = self._serialise_schema_row(scan_id, g)
            row_len = len(row) + 2
            if value_rows and (current_len + row_len) > self._MAX_SQL_BYTES:
                self._flush_group_rows(insert_prefix, value_rows)
                value_rows = []
                current_len = prefix_len
            value_rows.append(row)
            current_len += row_len
        if value_rows:
            self._flush_group_rows(insert_prefix, value_rows)

    def load_schema_groups(self) -> list[dict]:
        """Load schema duplicate pairs from the latest scan."""
        latest = self._latest_scan_id()
        if latest is None:
            return []
        rows = self._run_sql(
            f"SELECT result_json FROM {CACHE_SCHEMA}.schema_groups "
            f"WHERE scan_id = {latest}",
            quiet=True,
        )
        if not rows:
            return []
        return [json.loads(row[0]) for row in rows if row[0]]

    # ── Read cache ────────────────────────────────────────────────────────

    def load_scan_result(self) -> dict | None:
        """Load the scan-result summary from the latest scan."""
        latest = self._latest_scan_id()
        if latest is None:
            return None

        rows = self._run_sql(
            f"SELECT scan_result_json "
            f"FROM {CACHE_SCHEMA}.cache_metadata "
            f"WHERE scan_id = {latest}"
        )
        if not rows or not rows[0][0]:
            return None
        return json.loads(rows[0][0])

    def load_groups(self) -> list[dict]:
        """Load duplicate groups from the latest scan."""
        latest = self._latest_scan_id()
        if latest is None:
            return []

        rows = self._run_sql(
            f"SELECT group_id, label, tables_json, pairs_json, "
            f"       gold_standard, gold_scores_json, tags_json "
            f"FROM {CACHE_SCHEMA}.duplicate_groups "
            f"WHERE scan_id = {latest} "
            f"ORDER BY group_id"
        )
        if not rows:
            return []

        groups: list[dict] = []
        for row in rows:
            groups.append({
                "group_id": int(row[0]) if row[0] else 0,
                "label": row[1] or "",
                "tables": json.loads(row[2]) if row[2] else [],
                "pairs": json.loads(row[3]) if row[3] else [],
                "gold_standard": row[4] if row[4] else None,
                "gold_scores": json.loads(row[5]) if row[5] else {},
                "tags": json.loads(row[6]) if row[6] else [],
            })
        return groups
