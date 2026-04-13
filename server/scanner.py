"""Unity Catalog metadata scanner. Collects table/column info and permissions.

All metadata is read from snapshot tables in
``catalog_40_copper_uc_metadata.metadata`` — mirrors of
``system.information_schema`` that provide definer-rights access.

These snapshot tables are refreshed weekly by a scheduled CTAS job.
"""

from __future__ import annotations
import json
import logging
import threading
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional

from server.config import get_workspace_client, WAREHOUSE_ID

logger = logging.getLogger(__name__)


@dataclass
class ColumnInfo:
    name: str
    type_name: str
    position: int
    comment: Optional[str] = None
    nullable: bool = True


@dataclass
class PermissionGrant:
    principal: str
    privileges: list[str] = field(default_factory=list)
    inherited_from: Optional[str] = None


@dataclass
class TableInfo:
    catalog: str
    schema: str
    name: str
    full_name: str
    table_type: str
    columns: list[ColumnInfo] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    owner: Optional[str] = None
    comment: Optional[str] = None
    permissions: list[PermissionGrant] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def to_summary(self):
        """Lightweight dict for list views (no columns/permissions)."""
        return {
            "catalog": self.catalog,
            "schema": self.schema,
            "name": self.name,
            "full_name": self.full_name,
            "table_type": self.table_type,
            "owner": self.owner,
            "comment": self.comment,
            "column_count": len(self.columns),
        }


@dataclass
class SchemaInfo:
    catalog: str
    name: str
    full_name: str
    table_count: int = 0
    owner: Optional[str] = None
    comment: Optional[str] = None


class CatalogScanner:

    _METADATA_SOURCE = "catalog_40_copper_uc_metadata.metadata"

    # Catalogs to exclude from scan results
    _SKIP_CATALOG_NAMES = {"system", "samples", "__databricks_internal"}

    # SQL Statement API: max wait_timeout is 50s; poll beyond that
    _WAIT_TIMEOUT = "50s"
    _POLL_INTERVAL = 5       # seconds between polls
    _POLL_MAX_ATTEMPTS = 24  # 24 × 5s = 120s total

    _INITIAL_STATUS = {
        "state": "idle",          # idle | running | completed | failed
        "message": "",
        "current_catalog": None,
        "catalogs_done": 0,
        "catalogs_total": 0,
        "catalogs_scanned": [],
        "errors": [],
        "error": None,
        "result": None,
    }

    def __init__(self):
        self._client = None
        self._tables: list[TableInfo] = []
        self._schemas: list[SchemaInfo] = []
        self._scanned = False
        self._scanned_catalogs: list[str] = []
        self._duplicate_groups: list[dict] = []

        # Background scan state
        self._scan_lock = threading.Lock()
        self._scan_status: dict = dict(self._INITIAL_STATUS)

    @property
    def client(self):
        if self._client is None:
            self._client = get_workspace_client()
        return self._client

    def reset_client(self):
        """Reset the workspace client — skipped if a scan is active."""
        with self._scan_lock:
            if self._scan_status["state"] == "running":
                logger.info("Skipping reset_client: scan in progress")
                return
        self._client = None

    @property
    def is_scanned(self):
        return self._scanned

    @property
    def scanned_catalogs(self) -> list[str]:
        return list(self._scanned_catalogs)

    @property
    def last_scan_result(self) -> dict | None:
        """Return the most recent scan result (from scan or cache)."""
        with self._scan_lock:
            return self._scan_status.get("result")

    # ── Status helpers ────────────────────────────────────────────────────

    def _update_status(self, **kwargs):
        """Thread-safe update of one or more status fields."""
        with self._scan_lock:
            self._scan_status.update(kwargs)

    def _add_error(self, error_msg: str):
        """Append a non-fatal error to the status errors list."""
        with self._scan_lock:
            self._scan_status.setdefault("errors", []).append(error_msg)

    # ── Background scan control ───────────────────────────────────────────

    def start_scan(self) -> dict:
        """Launch scan_all in a background thread. Returns immediately.

        If a scan is already running, returns the current status instead
        of starting a duplicate.
        """
        with self._scan_lock:
            if self._scan_status["state"] == "running":
                return dict(self._scan_status)

            self._scan_status = {
                "state": "running",
                "message": "Initialising scan\u2026",
                "current_catalog": None,
                "catalogs_done": 0,
                "catalogs_total": 0,
                "catalogs_scanned": [],
                "errors": [],
                "error": None,
                "result": None,
            }

        thread = threading.Thread(target=self._run_scan_background, daemon=True)
        thread.start()
        return dict(self._scan_status)

    def get_scan_status(self) -> dict:
        """Return a snapshot of the current scan progress."""
        with self._scan_lock:
            # Return a copy including a copy of the errors list
            status = dict(self._scan_status)
            status["errors"] = list(self._scan_status.get("errors", []))
            status["catalogs_scanned"] = list(self._scan_status.get("catalogs_scanned", []))
            return status

    def _run_scan_background(self):
        """Wrapper that runs scan_all, detects duplicates, and writes cache."""
        try:
            result = self.scan_all()

            # ── Detect duplicates (runs in this background thread) ────────
            self._update_status(message="Detecting duplicates\u2026")
            try:
                from server.duplicates import detect_duplicates
                groups = detect_duplicates(self._tables)
                self._duplicate_groups = [g.to_dict() for g in groups]
                result["groups_count"] = len(self._duplicate_groups)
                logger.info(f"Detected {len(self._duplicate_groups)} duplicate groups")
            except Exception as e:
                logger.warning(f"Duplicate detection failed (non-fatal): {e}")
                self._duplicate_groups = []
                result["groups_count"] = 0
                self._add_error(f"Duplicate detection: {e}")

            # ── Write to UC cache (best-effort) ──────────────────────────
            self._update_status(message="Writing to cache\u2026")
            try:
                from server.cache import CacheManager
                cache_mgr = CacheManager(self)
                cache_mgr.write_cache(result, self._duplicate_groups)
            except Exception as e:
                logger.warning(f"Cache write failed (non-fatal): {e}")
                self._add_error(f"Cache write: {e}")

            # Sync any errors added after scan_all into the result
            with self._scan_lock:
                result["errors"] = list(self._scan_status.get("errors", []))
                self._scan_status["state"] = "completed"
                self._scan_status["message"] = "Scan complete"
                self._scan_status["result"] = result
        except Exception as e:
            logger.exception("Background scan failed")
            with self._scan_lock:
                self._scan_status["state"] = "failed"
                self._scan_status["message"] = f"Scan failed: {e}"
                self._scan_status["error"] = str(e)

    # ── SQL execution ─────────────────────────────────────────────────────

    def _run_sql(self, sql: str, quiet: bool = False) -> list[list] | None:
        """Execute a SQL statement via the SQL Statement API and return rows.

        Waits up to 50s inline; if the warehouse is still starting the
        query will be in PENDING/RUNNING state and we poll until it
        completes (up to ~120s total).
        """
        sql_preview = sql[:120].replace("\n", " ")
        try:
            raw = self.client.api_client.do(
                "POST",
                "/api/2.0/sql/statements/",
                body={
                    "statement": sql,
                    "warehouse_id": WAREHOUSE_ID,
                    "wait_timeout": self._WAIT_TIMEOUT,
                    "disposition": "EXTERNAL_LINKS",
                    "format": "JSON_ARRAY",
                },
            )
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            status = data.get("status", {})
            state = status.get("state")

            # Fast path: query completed within wait_timeout
            if state == "SUCCEEDED":
                return self._fetch_external_results(data)

            # Slow path: warehouse starting or query still running — poll
            statement_id = data.get("statement_id")
            if state in ("PENDING", "RUNNING") and statement_id:
                logger.info(f"Query state is {state}, polling... [query: {sql_preview}...]")
                self._update_status(message=f"Waiting for SQL warehouse\u2026")
                return self._poll_statement(statement_id, sql_preview)

            # Actual failure
            error_msg = status.get("error", {}).get("message", "no error details")
            msg = f"SQL state '{state}': {error_msg}"
            logger.warning(f"{msg} [query: {sql_preview}...]")
            if not quiet:
                self._add_error(msg)
        except Exception as e:
            msg = f"SQL exception: {e}"
            logger.warning(f"{msg} [query: {sql_preview}...]")
            if not quiet:
                self._add_error(msg)
        return None

    def _poll_statement(self, statement_id: str, sql_preview: str) -> list[list] | None:
        """Poll a running statement until it completes or times out."""
        for attempt in range(self._POLL_MAX_ATTEMPTS):
            time.sleep(self._POLL_INTERVAL)
            try:
                raw = self.client.api_client.do(
                    "GET",
                    f"/api/2.0/sql/statements/{statement_id}",
                )
                data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                state = data.get("status", {}).get("state")

                if state == "SUCCEEDED":
                    return self._fetch_external_results(data)

                if state in ("PENDING", "RUNNING"):
                    continue  # keep polling

                # Terminal non-success state (FAILED, CANCELED, CLOSED)
                error_msg = data.get("status", {}).get("error", {}).get("message", "no details")
                msg = f"SQL poll state '{state}': {error_msg}"
                logger.warning(f"{msg} [query: {sql_preview}...]")
                self._add_error(msg)
                return None
            except Exception as e:
                msg = f"SQL poll error: {e}"
                logger.warning(f"{msg} [statement: {statement_id}]")
                self._add_error(msg)
                return None

        msg = f"SQL timed out after {self._POLL_MAX_ATTEMPTS * self._POLL_INTERVAL}s"
        logger.warning(f"{msg} [query: {sql_preview}...]")
        self._add_error(msg)
        return None

    def _fetch_external_results(self, data: dict) -> list[list]:
        """Download result chunks from EXTERNAL_LINKS response."""
        all_rows = []
        result = data.get("result", {})

        while True:
            # Download each chunk from its presigned URL
            for link_info in result.get("external_links", []):
                url = link_info.get("external_link")
                if not url:
                    continue
                resp = urllib.request.urlopen(url)
                chunk_data = json.loads(resp.read().decode("utf-8"))
                if isinstance(chunk_data, list):
                    all_rows.extend(chunk_data)

            # Check for more pages of chunk metadata
            next_link = result.get("next_chunk_internal_link")
            if not next_link:
                break

            raw = self.client.api_client.do("GET", next_link)
            result = json.loads(raw) if isinstance(raw, (str, bytes)) else raw

        return all_rows

    # ── Catalog listing ───────────────────────────────────────────────────

    def list_catalogs(self) -> list[dict]:
        """Return all catalogs from the metadata snapshot, filtering system catalogs."""
        rows = self._run_sql(
            f"SELECT catalog_name, catalog_owner, comment "
            f"FROM {self._METADATA_SOURCE}.catalogs"
        )
        if not rows:
            return []

        result = []
        for row in rows:
            name = row[0]
            if name in self._SKIP_CATALOG_NAMES:
                continue
            result.append({
                "name": name,
                "owner": row[1],
                "comment": row[2],
            })
        return result

    # ── Full scan ─────────────────────────────────────────────────────────

    def scan_all(self) -> dict:
        """Scan every accessible catalog and accumulate results."""
        self.reset_client()
        self._tables = []
        self._schemas = []
        self._scanned_catalogs = []

        self._update_status(message="Listing catalogs\u2026")
        catalogs = self.list_catalogs()

        if not catalogs:
            # If SQL errors caused the empty result, fail loudly
            with self._scan_lock:
                sql_errors = list(self._scan_status.get("errors", []))
            if sql_errors:
                raise RuntimeError(
                    f"Catalog listing failed with {len(sql_errors)} SQL error(s): "
                    + "; ".join(sql_errors)
                )
            self._add_error(
                "No catalogs returned — the snapshot tables may be empty."
            )

        # Update status with total count now that we know it
        self._update_status(
            catalogs_total=len(catalogs),
            message=f"Found {len(catalogs)} catalogs to scan",
        )

        per_catalog = {}

        for cat_info in catalogs:
            name = cat_info["name"]

            # Report which catalog we're scanning
            self._update_status(
                current_catalog=name,
                message=f"Scanning {name}\u2026",
            )

            try:
                stats = self._scan_one(name)
                per_catalog[name] = stats
                self._scanned_catalogs.append(name)
            except Exception as e:
                logger.warning(f"Failed to scan catalog {name}: {e}")
                per_catalog[name] = {"error": str(e), "schema_count": 0,
                                     "table_count": 0, "column_count": 0}
                self._add_error(f"Catalog {name}: {e}")

            # Report progress after each catalog
            with self._scan_lock:
                self._scan_status["catalogs_done"] += 1
                self._scan_status["catalogs_scanned"] = list(self._scanned_catalogs)

        self._scanned = True

        with self._scan_lock:
            scan_errors = list(self._scan_status.get("errors", []))

        return {
            "catalogs_scanned": list(self._scanned_catalogs),
            "per_catalog": per_catalog,
            "total": {
                "catalog_count": len(self._scanned_catalogs),
                "schema_count": len(self._schemas),
                "table_count": len(self._tables),
                "column_count": sum(len(t.columns) for t in self._tables),
            },
            "errors": scan_errors,
        }

    def _scan_one(self, catalog: str) -> dict:
        """Scan a single catalog using the metadata snapshot tables."""
        logger.info(f"Scanning catalog: {catalog}")
        src = self._METADATA_SOURCE

        # ── Schemas ───────────────────────────────────────────────────────
        schema_rows = self._run_sql(
            f"SELECT schema_name, schema_owner, comment "
            f"FROM {src}.schemata "
            f"WHERE catalog_name = '{catalog}' "
            f"  AND schema_name != 'information_schema'"
        )

        local_schemas: list[SchemaInfo] = []
        if schema_rows:
            for row in schema_rows:
                local_schemas.append(SchemaInfo(
                    catalog=catalog,
                    name=row[0],
                    full_name=f"{catalog}.{row[0]}",
                    owner=row[1],
                    comment=row[2],
                ))

        # ── Tables ────────────────────────────────────────────────────────
        table_rows = self._run_sql(
            f"SELECT table_name, table_schema, table_type, "
            f"       table_owner, comment, created, last_altered "
            f"FROM {src}.tables "
            f"WHERE table_catalog = '{catalog}' "
            f"  AND table_schema != 'information_schema'"
        )

        local_tables: list[TableInfo] = []
        if table_rows:
            for row in table_rows:
                local_tables.append(TableInfo(
                    catalog=catalog,
                    schema=row[1],
                    name=row[0],
                    full_name=f"{catalog}.{row[1]}.{row[0]}",
                    table_type=row[2] or "UNKNOWN",
                    created_at=row[5],
                    updated_at=row[6],
                    owner=row[3],
                    comment=row[4],
                ))

        # ── Columns ───────────────────────────────────────────────────────
        col_rows = self._run_sql(
            f"SELECT table_schema, table_name, column_name, "
            f"       full_data_type, ordinal_position, is_nullable, comment "
            f"FROM {src}.columns "
            f"WHERE table_catalog = '{catalog}' "
            f"  AND table_schema != 'information_schema' "
            f"ORDER BY table_schema, table_name, ordinal_position"
        )

        if col_rows:
            col_lookup: dict[tuple, list[ColumnInfo]] = defaultdict(list)
            for row in col_rows:
                col_lookup[(row[0], row[1])].append(ColumnInfo(
                    name=row[2],
                    type_name=row[3] or "unknown",
                    position=int(row[4]) if row[4] else 0,
                    nullable=row[5] != "NO",
                    comment=row[6],
                ))
            for table in local_tables:
                table.columns = col_lookup.get((table.schema, table.name), [])

        # ── Schema table counts ───────────────────────────────────────────
        schema_table_counts: dict[str, int] = defaultdict(int)
        for t in local_tables:
            schema_table_counts[t.schema] += 1
        for s in local_schemas:
            s.table_count = schema_table_counts.get(s.name, 0)

        # ── Permissions ───────────────────────────────────────────────────
        self._fetch_permissions(catalog, local_tables)

        self._tables.extend(local_tables)
        self._schemas.extend(local_schemas)

        return {
            "catalog": catalog,
            "schema_count": len(local_schemas),
            "table_count": len(local_tables),
            "column_count": sum(len(t.columns) for t in local_tables),
        }

    # ── Permissions fetching ──────────────────────────────────────────────

    def _fetch_permissions(self, catalog: str, tables: list[TableInfo]):
        """Fetch permissions from the metadata snapshot tables."""
        source = self._METADATA_SOURCE
        logger.info(f"Permissions source for {catalog}: {source}")
        self._fetch_permissions_from(source, catalog, tables)

    def _fetch_permissions_from(
        self, source: str, catalog: str, tables: list[TableInfo]
    ):
        """Fetch catalog/schema/table privileges from the given source."""

        # ── Catalog-level grants ──────────────────────────────────────────
        catalog_grants: list[PermissionGrant] = []
        rows = self._run_sql(
            f"SELECT grantor, grantee, privilege_type "
            f"FROM {source}.catalog_privileges "
            f"WHERE catalog_name = '{catalog}'"
        )
        if rows:
            grouped: dict[str, list[str]] = defaultdict(list)
            for row in rows:
                grouped[row[1]].append(row[2])
            for principal, privs in grouped.items():
                catalog_grants.append(PermissionGrant(
                    principal=principal,
                    privileges=sorted(set(privs)),
                    inherited_from=catalog,
                ))

        # ── Schema-level grants ───────────────────────────────────────────
        schema_perms: dict[str, list[PermissionGrant]] = defaultdict(list)
        rows = self._run_sql(
            f"SELECT grantor, grantee, privilege_type, schema_name "
            f"FROM {source}.schema_privileges "
            f"WHERE catalog_name = '{catalog}'"
        )
        if rows:
            grouped_s: dict[tuple[str, str], list[str]] = defaultdict(list)
            for row in rows:
                grouped_s[(row[3], row[1])].append(row[2])
            for (schema_name, principal), privs in grouped_s.items():
                schema_perms[schema_name].append(PermissionGrant(
                    principal=principal,
                    privileges=sorted(set(privs)),
                    inherited_from=f"{catalog}.{schema_name}",
                ))

        # ── Table-level grants ────────────────────────────────────────────
        table_perms: dict[str, list[PermissionGrant]] = defaultdict(list)
        rows = self._run_sql(
            f"SELECT grantor, grantee, privilege_type, table_schema, table_name "
            f"FROM {source}.table_privileges "
            f"WHERE table_catalog = '{catalog}'"
        )
        if rows:
            grouped_t: dict[tuple[str, str, str], list[str]] = defaultdict(list)
            for row in rows:
                grouped_t[(row[3], row[4], row[1])].append(row[2])
            for (schema_name, table_name, principal), privs in grouped_t.items():
                table_perms[f"{schema_name}.{table_name}"].append(PermissionGrant(
                    principal=principal,
                    privileges=sorted(set(privs)),
                    inherited_from=f"{catalog}.{schema_name}.{table_name}",
                ))

        # ── Merge onto tables ─────────────────────────────────────────────
        self._merge_permissions(tables, catalog_grants, schema_perms, table_perms)

    def _merge_permissions(
        self,
        tables: list[TableInfo],
        catalog_grants: list[PermissionGrant],
        schema_perms: dict[str, list[PermissionGrant]],
        table_perms: dict[str, list[PermissionGrant]],
    ):
        """Merge catalog, schema, and table grants onto each table."""
        for table in tables:
            merged: dict[str, PermissionGrant] = {}

            for g in catalog_grants:
                merged[g.principal] = PermissionGrant(
                    principal=g.principal,
                    privileges=list(g.privileges),
                    inherited_from=g.inherited_from,
                )

            for g in schema_perms.get(table.schema, []):
                if g.principal in merged:
                    existing = set(merged[g.principal].privileges)
                    existing.update(g.privileges)
                    merged[g.principal].privileges = sorted(existing)
                    merged[g.principal].inherited_from = g.inherited_from
                else:
                    merged[g.principal] = PermissionGrant(
                        principal=g.principal,
                        privileges=list(g.privileges),
                        inherited_from=g.inherited_from,
                    )

            tkey = f"{table.schema}.{table.name}"
            for g in table_perms.get(tkey, []):
                if g.principal in merged:
                    existing = set(merged[g.principal].privileges)
                    existing.update(g.privileges)
                    merged[g.principal].privileges = sorted(existing)
                    merged[g.principal].inherited_from = g.inherited_from
                else:
                    merged[g.principal] = PermissionGrant(
                        principal=g.principal,
                        privileges=list(g.privileges),
                        inherited_from=g.inherited_from,
                    )

            table.permissions = list(merged.values())

    def get_schemas(self, catalog: str | None = None) -> list[dict]:
        schemas = self._schemas
        if catalog:
            schemas = [s for s in schemas if s.catalog == catalog]
        return [
            {
                "catalog": s.catalog,
                "name": s.name,
                "full_name": s.full_name,
                "table_count": s.table_count,
                "owner": s.owner,
                "comment": s.comment,
            }
            for s in schemas
        ]

    def get_tables(self, schema: str | None = None, catalog: str | None = None) -> list[dict]:
        tables = self._tables
        if catalog:
            tables = [t for t in tables if t.catalog == catalog]
        if schema:
            tables = [t for t in tables if t.schema == schema]
        return [t.to_summary() for t in tables]

    def get_table_by_full_name(self, catalog: str, schema: str, table: str) -> dict | None:
        for t in self._tables:
            if t.catalog == catalog and t.schema == schema and t.name == table:
                # Lazy-load columns if not yet populated (cache-restore path)
                if not t.columns:
                    t.columns = self.load_table_columns(catalog, schema, table)
                    self.load_table_permissions(catalog, t)
                return t.to_dict()
        return None

    def get_table_raw(self, catalog: str, schema: str, table: str) -> TableInfo | None:
        for t in self._tables:
            if t.catalog == catalog and t.schema == schema and t.name == table:
                return t
        return None

    def get_duplicate_groups(self) -> list[dict]:
        """Return pre-computed duplicate groups."""
        return self._duplicate_groups

    def set_duplicate_groups(self, groups: list[dict]):
        """Set duplicate groups (used by cache-load)."""
        self._duplicate_groups = groups

    def get_all_tables_raw(self) -> list[TableInfo]:
        return self._tables

    # ── Bulk loading (fast startup from cache) ────────────────────────────

    def _skip_catalogs_sql(self) -> str:
        """Return comma-separated quoted catalog names for SQL IN clause."""
        return ", ".join(f"'{c}'" for c in self._SKIP_CATALOG_NAMES)

    def bulk_load_tables(self):
        """Load all tables (WITHOUT columns/permissions) in one query.

        Much faster than per-catalog scanning — used when restoring from
        cache where duplicate groups are already computed.
        """
        src = self._METADATA_SOURCE
        skip = self._skip_catalogs_sql()
        rows = self._run_sql(
            f"SELECT table_catalog, table_schema, table_name, table_type, "
            f"       table_owner, comment, created, last_altered "
            f"FROM {src}.tables "
            f"WHERE table_schema != 'information_schema' "
            f"  AND table_catalog NOT IN ({skip}) "
            f"ORDER BY table_catalog, table_schema, table_name"
        )
        if not rows:
            return

        self._tables = []
        for row in rows:
            self._tables.append(TableInfo(
                catalog=row[0],
                schema=row[1],
                name=row[2],
                full_name=f"{row[0]}.{row[1]}.{row[2]}",
                table_type=row[3] or "UNKNOWN",
                created_at=row[6],
                updated_at=row[7],
                owner=row[4],
                comment=row[5],
            ))
        logger.info(f"Bulk-loaded {len(self._tables)} tables")

    def bulk_load_schemas(self):
        """Load all schemas in one query and compute table counts."""
        src = self._METADATA_SOURCE
        skip = self._skip_catalogs_sql()
        rows = self._run_sql(
            f"SELECT catalog_name, schema_name, schema_owner, comment "
            f"FROM {src}.schemata "
            f"WHERE schema_name != 'information_schema' "
            f"  AND catalog_name NOT IN ({skip}) "
            f"ORDER BY catalog_name, schema_name"
        )
        if not rows:
            return

        self._schemas = []
        for row in rows:
            self._schemas.append(SchemaInfo(
                catalog=row[0],
                name=row[1],
                full_name=f"{row[0]}.{row[1]}",
                owner=row[2],
                comment=row[3],
            ))

        # Compute table counts from the already-loaded tables
        schema_counts: dict[str, int] = defaultdict(int)
        for t in self._tables:
            schema_counts[f"{t.catalog}.{t.schema}"] += 1
        for s in self._schemas:
            s.table_count = schema_counts.get(s.full_name, 0)

        logger.info(f"Bulk-loaded {len(self._schemas)} schemas")

    def load_table_columns(self, catalog: str, schema: str, name: str) -> list[ColumnInfo]:
        """Fetch columns for a single table on demand."""
        src = self._METADATA_SOURCE
        rows = self._run_sql(
            f"SELECT column_name, full_data_type, ordinal_position, "
            f"       is_nullable, comment "
            f"FROM {src}.columns "
            f"WHERE table_catalog = '{catalog}' "
            f"  AND table_schema = '{schema}' "
            f"  AND table_name = '{name}' "
            f"ORDER BY ordinal_position"
        )
        if not rows:
            return []

        columns: list[ColumnInfo] = []
        for row in rows:
            columns.append(ColumnInfo(
                name=row[0],
                type_name=row[1] or "unknown",
                position=int(row[2]) if row[2] else 0,
                nullable=row[3] != "NO",
                comment=row[4],
            ))
        return columns

    def load_table_permissions(self, catalog: str, table_obj: "TableInfo"):
        """Fetch and merge permissions for a single table on demand."""
        self._fetch_permissions(catalog, [table_obj])

    def load_from_cache(self, scan_result: dict):
        """Restore scanner state from cache + bulk metadata queries.

        Loads tables and schemas from the metadata snapshot (fast, 2
        queries total) and marks the scanner as ready.  Duplicate groups
        are loaded separately by the route layer.
        """
        self._update_status(state="running", message="Loading tables\u2026")
        self.bulk_load_tables()

        self._update_status(message="Loading schemas\u2026")
        self.bulk_load_schemas()

        self._scanned_catalogs = scan_result.get("catalogs_scanned", [])
        self._scanned = True

        self._update_status(
            state="completed",
            message="Loaded from cache",
            result=scan_result,
        )
        logger.info("Scanner state restored from cache")


scanner = CatalogScanner()
