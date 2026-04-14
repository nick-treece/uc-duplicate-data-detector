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
2. ``CACHE_VERSION`` in code differs from the stored version (covers
   schema changes to the cache tables during development).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.scanner import CatalogScanner

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────
CACHE_SCHEMA = "catalog_40_copper_uc_metadata.cache"
CACHE_VERSION = "3"
CACHE_MAX_AGE_DAYS = 7


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

        self._run_sql(f"""
            CREATE TABLE IF NOT EXISTS {CACHE_SCHEMA}.cache_metadata (
                scan_id          INT,
                cache_version    STRING,
                cached_at        TIMESTAMP,
                scan_result_json STRING
            )
        """)

        self._run_sql(f"""
            CREATE TABLE IF NOT EXISTS {CACHE_SCHEMA}.duplicate_groups (
                scan_id          INT,
                group_id         INT,
                label            STRING,
                tables_json      STRING,
                pairs_json       STRING,
                gold_standard    STRING,
                gold_scores_json STRING,
                tags_json        STRING
            )
        """)

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

    def write_cache(self, scan_result: dict, groups: list[dict]):
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

        logger.info(
            f"Cache written \u2014 scan_id={scan_id}, "
            f"{len(groups)} duplicate group(s)"
        )

    def _write_groups_batched(
        self, scan_id: int, groups: list[dict], batch_size: int = 50
    ):
        """INSERT groups in batches to stay within SQL size limits."""
        for i in range(0, len(groups), batch_size):
            batch = groups[i : i + batch_size]
            value_rows: list[str] = []

            for g in batch:
                gid = int(g["group_id"])
                label = self._esc(g.get("label", ""))
                tables = self._esc(json.dumps(g.get("tables", [])))
                pairs = self._esc(
                    json.dumps(g.get("pairs", []), default=str)
                )
                gold = self._esc(g.get("gold_standard") or "")
                scores = self._esc(
                    json.dumps(g.get("gold_scores", {}), default=str)
                )
                tags = self._esc(
                    json.dumps(g.get("tags", []))
                )
                value_rows.append(
                    f"({scan_id}, {gid}, '{label}', '{tables}', "
                    f"'{pairs}', '{gold}', '{scores}', '{tags}')"
                )

            sql = (
                f"INSERT INTO {CACHE_SCHEMA}.duplicate_groups VALUES "
                + ", ".join(value_rows)
            )
            self._run_sql(sql)

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
