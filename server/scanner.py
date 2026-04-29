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
from collections import defaultdict, deque
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


@dataclass
class LineageEdge:
    """A distinct source \u2192 target relationship from table_lineage."""
    source_table: str
    target_table: str
    entity_types: set[str] = field(default_factory=set)


class CatalogScanner:

    _METADATA_SOURCE = "catalog_40_copper_uc_metadata.metadata"

    _SKIP_CATALOG_NAMES = {"system", "samples", "__databricks_internal"}

    _WAIT_TIMEOUT = "50s"
    _POLL_INTERVAL = 5
    _POLL_MAX_ATTEMPTS = 24

    _INITIAL_STATUS = {
        "state": "idle",
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

        # Lineage data
        self._lineage_edges: list[LineageEdge] = []
        self._upstream_map: dict[str, set[str]] = {}
        self._downstream_map: dict[str, set[str]] = {}
        self._consumer_counts: dict[str, int] = {}
        self._column_lineage_map: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._transitive_upstream: dict[str, dict[str, int]] = {}  # table → {ancestor: min_depth}
        self._transitive_upstream: dict[str, dict[str, int]] = {}

        # Background scan state
        self._scan_lock = threading.Lock()
        self._scan_status: dict = dict(self._INITIAL_STATUS)

    @property
    def client(self):
        if self._client is None:
            self._client = get_workspace_client()
        return self._client

    def reset_client(self):
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
        with self._scan_lock:
            return self._scan_status.get("result")

    def _update_status(self, **kwargs):
        with self._scan_lock:
            self._scan_status.update(kwargs)

    def _add_error(self, error_msg: str):
        with self._scan_lock:
            self._scan_status.setdefault("errors", []).append(error_msg)

    def start_scan(self) -> dict:
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
                "_start_time": time.time(),
            }
        thread = threading.Thread(target=self._run_scan_background, daemon=True)
        thread.start()
        return dict(self._scan_status)

    def get_scan_status(self) -> dict:
        with self._scan_lock:
            status = dict(self._scan_status)
            status["errors"] = list(self._scan_status.get("errors", []))
            status["catalogs_scanned"] = list(self._scan_status.get("catalogs_scanned", []))
            # Dynamically append elapsed time when running
            start_time = self._scan_status.get("_start_time")
            if status.get("state") == "running" and start_time:
                elapsed = int(time.time() - start_time)
                status["message"] = f"{status['message']} ({elapsed}s)"
            # Don't expose internal field to frontend
            status.pop("_start_time", None)
            return status

    def _run_scan_background(self):
        """Wrapper that runs scan_all, loads lineage, detects duplicates, and writes cache."""
        try:
            result = self.scan_all()

            # \u2500\u2500 Load lineage data \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
            try:
                self.bulk_load_table_lineage()
                self.bulk_load_column_lineage()
            except Exception as e:
                logger.warning(f"Lineage loading failed (non-fatal): {e}")
                self._add_error(f"Lineage: {e}")

            # \u2500\u2500 Detect duplicates \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
            self._update_status(message="Detecting duplicates\u2026")
            try:
                from server.duplicates import detect_duplicates
                lineage_ctx = {
                    "upstream_map": self._upstream_map,
                    "downstream_map": self._downstream_map,
                    "consumer_counts": self._consumer_counts,
                    "column_lineage_map": self._column_lineage_map,
                    "transitive_upstream": self._transitive_upstream,
                }
                groups = detect_duplicates(self._tables, lineage=lineage_ctx)
                self._duplicate_groups = [g.to_dict() for g in groups]
                result["groups_count"] = len(self._duplicate_groups)
                logger.info(f"Detected {len(self._duplicate_groups)} duplicate groups")
            except Exception as e:
                logger.warning(f"Duplicate detection failed (non-fatal): {e}")
                self._duplicate_groups = []
                result["groups_count"] = 0
                self._add_error(f"Duplicate detection: {e}")

            # \u2500\u2500 Write to UC cache \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
            self._update_status(message="Writing to cache\u2026")
            try:
                from server.cache import CacheManager
                cache_mgr = CacheManager(self)
                cache_mgr.write_cache(result, self._duplicate_groups)
            except Exception as e:
                logger.warning(f"Cache write failed (non-fatal): {e}")
                self._add_error(f"Cache write: {e}")

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

    # \u2500\u2500 SQL execution \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _run_sql(self, sql: str, quiet: bool = False) -> list[list] | None:
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

            if state == "SUCCEEDED":
                return self._fetch_external_results(data)

            statement_id = data.get("statement_id")
            if state in ("PENDING", "RUNNING") and statement_id:
                logger.info(f"Query state is {state}, polling... [query: {sql_preview}...]")
                self._update_status(message=f"Waiting for SQL warehouse\u2026")
                return self._poll_statement(statement_id, sql_preview)

            error_msg = status.get("error", {}).get("message", "no error details")
            msg = f"SQL state '{state}': {error_msg}"
            logger.warning(f"{msg} [query: {sql_preview}...]")
            if not quiet:
                self._add_error(msg)
        except Exception as e:
            msg = f"SQL exception ({type(e).__name__}): {e} [query: {sql_preview[:80]}]"
            logger.warning(msg)
            if not quiet:
                self._add_error(msg)
        return None

    def _poll_statement(self, statement_id: str, sql_preview: str) -> list[list] | None:
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
                    continue
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
        all_rows = []
        result = data.get("result", {})
        while True:
            next_link = None
            for link_info in result.get("external_links", []):
                url = link_info.get("external_link")
                if not url:
                    continue
                resp = urllib.request.urlopen(url)
                chunk_data = json.loads(resp.read().decode("utf-8"))
                if isinstance(chunk_data, list):
                    all_rows.extend(chunk_data)
                next_link = link_info.get("next_chunk_internal_link")
            if not next_link:
                break
            raw = self.client.api_client.do("GET", next_link)
            result = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return all_rows

    # \u2500\u2500 Catalog listing \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def list_catalogs(self) -> list[dict]:
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
            result.append({"name": name, "owner": row[1], "comment": row[2]})
        return result

    # \u2500\u2500 Full scan \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def scan_all(self) -> dict:
        self.reset_client()
        self._tables = []
        self._schemas = []
        self._scanned_catalogs = []

        self._update_status(message="Listing catalogs\u2026")
        catalogs = self.list_catalogs()

        if not catalogs:
            with self._scan_lock:
                sql_errors = list(self._scan_status.get("errors", []))
            if sql_errors:
                raise RuntimeError(
                    f"Catalog listing failed with {len(sql_errors)} SQL error(s): "
                    + "; ".join(sql_errors)
                )
            self._add_error("No catalogs returned \u2014 the snapshot tables may be empty.")

        self._update_status(
            catalogs_total=len(catalogs),
            message=f"Found {len(catalogs)} catalogs to scan",
        )

        per_catalog = {}
        for cat_info in catalogs:
            name = cat_info["name"]
            self._update_status(current_catalog=name, message=f"Scanning {name}\u2026")
            try:
                stats = self._scan_one(name)
                per_catalog[name] = stats
                self._scanned_catalogs.append(name)
            except Exception as e:
                logger.warning(f"Failed to scan catalog {name}: {e}")
                per_catalog[name] = {"error": str(e), "schema_count": 0,
                                     "table_count": 0, "column_count": 0}
                self._add_error(f"Catalog {name}: {e}")
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
        logger.info(f"Scanning catalog: {catalog}")
        src = self._METADATA_SOURCE

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
                    catalog=catalog, name=row[0],
                    full_name=f"{catalog}.{row[0]}",
                    owner=row[1], comment=row[2],
                ))

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
                    catalog=catalog, schema=row[1], name=row[0],
                    full_name=f"{catalog}.{row[1]}.{row[0]}",
                    table_type=row[2] or "UNKNOWN",
                    created_at=row[5], updated_at=row[6],
                    owner=row[3], comment=row[4],
                ))

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
                    name=row[2], type_name=row[3] or "unknown",
                    position=int(row[4]) if row[4] else 0,
                    nullable=row[5] != "NO", comment=row[6],
                ))
            for table in local_tables:
                table.columns = col_lookup.get((table.schema, table.name), [])

        schema_table_counts: dict[str, int] = defaultdict(int)
        for t in local_tables:
            schema_table_counts[t.schema] += 1
        for s in local_schemas:
            s.table_count = schema_table_counts.get(s.name, 0)

        self._fetch_permissions(catalog, local_tables)
        self._tables.extend(local_tables)
        self._schemas.extend(local_schemas)

        return {
            "catalog": catalog,
            "schema_count": len(local_schemas),
            "table_count": len(local_tables),
            "column_count": sum(len(t.columns) for t in local_tables),
        }

    # \u2500\u2500 Permissions fetching \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _fetch_permissions(self, catalog: str, tables: list[TableInfo]):
        source = self._METADATA_SOURCE
        logger.info(f"Permissions source for {catalog}: {source}")
        self._fetch_permissions_from(source, catalog, tables)

    def _fetch_permissions_from(self, source: str, catalog: str, tables: list[TableInfo]):
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
                    principal=principal, privileges=sorted(set(privs)),
                    inherited_from=catalog,
                ))

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
                    principal=principal, privileges=sorted(set(privs)),
                    inherited_from=f"{catalog}.{schema_name}",
                ))

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
                    principal=principal, privileges=sorted(set(privs)),
                    inherited_from=f"{catalog}.{schema_name}.{table_name}",
                ))

        self._merge_permissions(tables, catalog_grants, schema_perms, table_perms)

    def _merge_permissions(self, tables, catalog_grants, schema_perms, table_perms):
        for table in tables:
            merged: dict[str, PermissionGrant] = {}
            for g in catalog_grants:
                merged[g.principal] = PermissionGrant(
                    principal=g.principal, privileges=list(g.privileges),
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
                        principal=g.principal, privileges=list(g.privileges),
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
                        principal=g.principal, privileges=list(g.privileges),
                        inherited_from=g.inherited_from,
                    )
            table.permissions = list(merged.values())

    def get_schemas(self, catalog: str | None = None) -> list[dict]:
        schemas = self._schemas
        if catalog:
            schemas = [s for s in schemas if s.catalog == catalog]
        return [{"catalog": s.catalog, "name": s.name, "full_name": s.full_name,
                 "table_count": s.table_count, "owner": s.owner, "comment": s.comment}
                for s in schemas]

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
        return self._duplicate_groups

    def set_duplicate_groups(self, groups: list[dict]):
        self._duplicate_groups = groups

    def get_all_tables_raw(self) -> list[TableInfo]:
        return self._tables

    # \u2500\u2500 Background re-detection \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def start_detection(self, threshold: float = 0.5) -> dict:
        with self._scan_lock:
            if self._scan_status["state"] == "running":
                return {"state": "busy", "message": "A scan is already running"}
        self._update_status(state="running", message="Detecting duplicates\u2026", phase="detection", _start_time=time.time())
        t = threading.Thread(
            target=self._run_detection_background, args=(threshold,), daemon=True,
        )
        t.start()
        return {"state": "started", "message": "Detection started"}

    def _run_detection_background(self, threshold: float):
        try:
            t0 = time.time()

            cols_loaded = sum(1 for t in self._tables if t.columns)
            if cols_loaded < len(self._tables) * 0.9:
                self._update_status(
                    message=f"Loading column metadata ({cols_loaded}/{len(self._tables)} loaded)\u2026"
                )
                self.bulk_load_columns()

            from server.duplicates import detect_duplicates

            if not self._lineage_edges:
                try:
                    self.bulk_load_table_lineage()
                    self.bulk_load_column_lineage()
                except Exception:
                    pass

            lineage_ctx = {
                "upstream_map": self._upstream_map,
                "downstream_map": self._downstream_map,
                "consumer_counts": self._consumer_counts,
                "column_lineage_map": self._column_lineage_map,
                "transitive_upstream": self._transitive_upstream,
            }

            self._update_status(message=f"Comparing tables\u2026")
            groups = detect_duplicates(self._tables, threshold=threshold, lineage=lineage_ctx)
            self._duplicate_groups = [g.to_dict() for g in groups]

            elapsed = int(time.time() - t0)
            with self._scan_lock:
                self._scan_status["state"] = "completed"
                self._scan_status["message"] = (
                    f"Detection complete \u2014 {len(self._duplicate_groups)} groups ({elapsed}s)"
                )
                self._scan_status["result"] = {"groups_count": len(self._duplicate_groups)}
        except Exception as e:
            logger.exception("Background detection failed")
            with self._scan_lock:
                self._scan_status["state"] = "failed"
                self._scan_status["message"] = f"Detection failed: {e}"
                self._scan_status["error"] = str(e)

    # \u2500\u2500 Bulk loading (fast startup from cache) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _skip_catalogs_sql(self) -> str:
        return ", ".join(f"'{c}'" for c in self._SKIP_CATALOG_NAMES)

    def bulk_load_tables(self):
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
                catalog=row[0], schema=row[1], name=row[2],
                full_name=f"{row[0]}.{row[1]}.{row[2]}",
                table_type=row[3] or "UNKNOWN",
                created_at=row[6], updated_at=row[7],
                owner=row[4], comment=row[5],
            ))
        logger.info(f"Bulk-loaded {len(self._tables)} tables")

    def bulk_load_columns(self):
        src = self._METADATA_SOURCE
        catalogs: dict[str, list[TableInfo]] = defaultdict(list)
        for t in self._tables:
            catalogs[t.catalog].append(t)

        total_cats = len(catalogs)
        total_cols = 0
        for i, (catalog, cat_tables) in enumerate(sorted(catalogs.items())):
            if i % 10 == 0:
                self._update_status(message=f"Loading columns\u2026 {i}/{total_cats} catalogs")
                time.sleep(0)

            col_rows = self._run_sql(
                f"SELECT table_schema, table_name, column_name, "
                f"       full_data_type, ordinal_position, is_nullable, comment "
                f"FROM {src}.columns "
                f"WHERE table_catalog = '{catalog}' "
                f"  AND table_schema != 'information_schema' "
                f"ORDER BY table_schema, table_name, ordinal_position",
                quiet=True,
            )
            if not col_rows:
                continue
            col_lookup: dict[tuple, list[ColumnInfo]] = defaultdict(list)
            for row in col_rows:
                col_lookup[(row[0], row[1])].append(ColumnInfo(
                    name=row[2], type_name=row[3] or "unknown",
                    position=int(row[4]) if row[4] else 0,
                    nullable=row[5] != "NO", comment=row[6],
                ))
            for table in cat_tables:
                key = (table.schema, table.name)
                if key in col_lookup:
                    table.columns = col_lookup[key]
                    total_cols += len(col_lookup[key])

        logger.info(f"bulk_load_columns: {total_cols} columns loaded across {total_cats} catalogs")

    def bulk_load_schemas(self):
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
                catalog=row[0], name=row[1],
                full_name=f"{row[0]}.{row[1]}",
                owner=row[2], comment=row[3],
            ))
        schema_counts: dict[str, int] = defaultdict(int)
        for t in self._tables:
            schema_counts[f"{t.catalog}.{t.schema}"] += 1
        for s in self._schemas:
            s.table_count = schema_counts.get(s.full_name, 0)
        logger.info(f"Bulk-loaded {len(self._schemas)} schemas")

    # \u2500\u2500 Lineage loading \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def bulk_load_table_lineage(self):
        """Load distinct table-level lineage edges from the last 90 days."""
        src = self._METADATA_SOURCE
        self._update_status(message="Loading table lineage\u2026")

        edge_rows = self._run_sql(
            f"SELECT DISTINCT source_table_full_name, target_table_full_name, entity_type "
            f"FROM {src}.table_lineage "
            f"WHERE event_date >= DATEADD(DAY, -90, CURRENT_DATE()) "
            f"  AND source_table_full_name IS NOT NULL "
            f"  AND target_table_full_name IS NOT NULL",
            quiet=True,
        )
        raw_edges: dict[tuple[str, str], set[str]] = defaultdict(set)
        if edge_rows:
            for row in edge_rows:
                source = row[0].lower()
                target = row[1].lower()
                entity_type = row[2] or "UNKNOWN"
                raw_edges[(source, target)].add(entity_type)

        self._lineage_edges = [
            LineageEdge(source_table=s, target_table=t, entity_types=e)
            for (s, t), e in raw_edges.items()
        ]
        self._build_lineage_lookups()
        self._build_transitive_upstream()

        consumer_rows = self._run_sql(
            f"SELECT source_table_full_name, COUNT(DISTINCT entity_id) AS consumer_count "
            f"FROM {src}.table_lineage "
            f"WHERE event_date >= DATEADD(DAY, -90, CURRENT_DATE()) "
            f"  AND source_table_full_name IS NOT NULL "
            f"  AND entity_id IS NOT NULL "
            f"GROUP BY source_table_full_name",
            quiet=True,
        )
        self._consumer_counts = {}
        if consumer_rows:
            for row in consumer_rows:
                self._consumer_counts[row[0].lower()] = int(row[1])

        # Build transitive ancestor map (depth-capped BFS)
        self._build_transitive_upstream()

        logger.info(
            f"Loaded {len(self._lineage_edges)} lineage edges, "
            f"{len(self._consumer_counts)} tables with consumer counts, "
            f"{len(self._transitive_upstream)} tables with transitive ancestors"
        )

    def _build_lineage_lookups(self):
        self._upstream_map = defaultdict(set)
        self._downstream_map = defaultdict(set)
        for edge in self._lineage_edges:
            self._upstream_map[edge.target_table].add(edge.source_table)
            self._downstream_map[edge.source_table].add(edge.target_table)

    def _build_transitive_upstream(
        self, max_depth: int = 5, max_ancestors: int = 500,
    ):
        """Compute transitive ancestors for every table via depth-capped BFS.

        Walks ``_upstream_map`` edges up to *max_depth* hops, storing the
        shortest path depth to each ancestor.  Tables with fan-in above
        the norm (hub tables) are capped at *max_ancestors* to prevent
        memory/time blow-up.

        Result is stored in ``_transitive_upstream``:
            {table: {ancestor: min_depth, ...}}
        """
        self._update_status(message="Building transitive ancestry\u2026")
        upstream = self._upstream_map
        result: dict[str, dict[str, int]] = {}

        for table in upstream:
            ancestors: dict[str, int] = {}
            visited: set[str] = {table}
            queue: deque[tuple[str, int]] = deque()

            # Seed with direct parents (depth 1)
            for parent in upstream.get(table, ()):
                if parent not in visited:
                    queue.append((parent, 1))
                    visited.add(parent)

            while queue:
                node, depth = queue.popleft()

                # Record this ancestor at the shortest depth seen
                if node not in ancestors or depth < ancestors[node]:
                    ancestors[node] = depth

                # Stop expanding if we've hit the caps
                if len(ancestors) >= max_ancestors:
                    break
                if depth >= max_depth:
                    continue

                # Expand to grandparents
                for grandparent in upstream.get(node, ()):
                    if grandparent not in visited:
                        visited.add(grandparent)
                        queue.append((grandparent, depth + 1))

            if ancestors:
                result[table] = ancestors

        self._transitive_upstream = result
        logger.info(
            f"Transitive upstream: {len(result)} tables, "
            f"avg {sum(len(v) for v in result.values()) / max(len(result), 1):.1f} "
            f"ancestors/table"
        )


    def _build_transitive_upstream(self, max_depth: int = 3, max_ancestors: int = 200):
        """Compute transitive ancestors for every table via depth-capped BFS.

        Walks ``_upstream_map`` edges up to *max_depth* hops.  For each table
        stores ``{ancestor_table: shortest_path_depth}``.  Hub tables (with
        very high fan-in) are capped at *max_ancestors* to avoid explosion.
        """
        from collections import deque

        self._transitive_upstream = {}
        upstream = self._upstream_map

        for table in upstream:
            ancestors: dict[str, int] = {}
            queue: deque[tuple[str, int]] = deque()

            # Seed with direct parents (depth 1)
            for parent in upstream.get(table, set()):
                if parent != table:
                    ancestors[parent] = 1
                    queue.append((parent, 1))

            # BFS up the graph
            while queue and len(ancestors) < max_ancestors:
                current, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                for grandparent in upstream.get(current, set()):
                    if grandparent == table:
                        continue
                    existing = ancestors.get(grandparent)
                    if existing is None or depth + 1 < existing:
                        ancestors[grandparent] = depth + 1
                        queue.append((grandparent, depth + 1))

            if ancestors:
                self._transitive_upstream[table] = ancestors

        logger.info(
            f"Transitive upstream: {len(self._transitive_upstream)} tables "
            f"with ancestors (max_depth={max_depth})"
        )

    def bulk_load_column_lineage(self):
        """No-op: column lineage is loaded on demand via query_column_lineage().

        The full column_lineage table has millions of distinct mappings,
        too many to pre-load via the SQL Statement API.  Instead,
        column mappings are queried for specific table pairs when needed
        (e.g. on the Compare page).
        """
        pass

    def query_column_lineage(
        self, table_a: str, table_b: str,
    ) -> list[dict]:
        """Query column-level lineage between two specific tables (on demand).

        Returns a list of {source_table, source_col, target_table, target_col}
        dicts for both directions (A\u2192B and B\u2192A).
        """
        src = self._METADATA_SOURCE
        a_escaped = table_a.replace("'", "''")
        b_escaped = table_b.replace("'", "''")

        rows = self._run_sql(
            f"SELECT DISTINCT source_table_full_name, source_column_name, "
            f"       target_table_full_name, target_column_name "
            f"FROM {src}.column_lineage "
            f"WHERE event_date >= DATEADD(DAY, -90, CURRENT_DATE()) "
            f"  AND source_column_name IS NOT NULL "
            f"  AND target_column_name IS NOT NULL "
            f"  AND ("
            f"    (source_table_full_name = '{a_escaped}' AND target_table_full_name = '{b_escaped}')"
            f"    OR "
            f"    (source_table_full_name = '{b_escaped}' AND target_table_full_name = '{a_escaped}')"
            f"  )",
            quiet=True,
        )

        mappings = []
        if rows:
            for row in rows:
                mappings.append({
                    "source_table": row[0],
                    "source_col": row[1].lower(),
                    "target_table": row[2],
                    "target_col": row[3].lower(),
                })
        return mappings

    @property
    def upstream_map(self) -> dict[str, set[str]]:
        return dict(self._upstream_map)

    @property
    def downstream_map(self) -> dict[str, set[str]]:
        return dict(self._downstream_map)

    @property
    def consumer_counts(self) -> dict[str, int]:
        return dict(self._consumer_counts)

    @property
    def lineage_edges(self) -> list[LineageEdge]:
        return list(self._lineage_edges)

    @property
    def transitive_upstream(self) -> dict[str, dict[str, int]]:
        return dict(self._transitive_upstream)

    @property
    def column_lineage_map(self) -> dict[tuple[str, str], list[tuple[str, str]]]:
        return dict(self._column_lineage_map)

    @property
    def transitive_upstream(self) -> dict[str, dict[str, int]]:
        return dict(self._transitive_upstream)

    # \u2500\u2500 Single-table loaders \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def load_table_columns(self, catalog: str, schema: str, name: str) -> list[ColumnInfo]:
        src = self._METADATA_SOURCE
        rows = self._run_sql(
            f"SELECT column_name, full_data_type, ordinal_position, is_nullable, comment "
            f"FROM {src}.columns "
            f"WHERE table_catalog = '{catalog}' AND table_schema = '{schema}' "
            f"  AND table_name = '{name}' ORDER BY ordinal_position"
        )
        if not rows:
            return []
        return [ColumnInfo(
            name=row[0], type_name=row[1] or "unknown",
            position=int(row[2]) if row[2] else 0,
            nullable=row[3] != "NO", comment=row[4],
        ) for row in rows]

    def load_table_permissions(self, catalog: str, table_obj: "TableInfo"):
        self._fetch_permissions(catalog, [table_obj])

    def load_from_cache(self, scan_result: dict):
        """Restore scanner state from cache + bulk metadata queries."""
        self._update_status(state="running", message="Loading tables\u2026")
        self.bulk_load_tables()

        self._update_status(message="Loading schemas\u2026")
        self.bulk_load_schemas()

        try:
            self.bulk_load_table_lineage()
            self.bulk_load_column_lineage()
        except Exception as e:
            logger.warning(f"Lineage loading failed (non-fatal): {e}")

        self._scanned_catalogs = scan_result.get("catalogs_scanned", [])
        self._scanned = True
        self._update_status(state="completed", message="Loaded from cache", result=scan_result)
        logger.info("Scanner state restored from cache")


    def query_group_members(self, group_names: list[str]) -> dict[str, list[dict]]:
        """Query users_and_groups for members of the given groups.

        Returns {group_name: [{name, email}, ...]} for each group.
        """
        if not group_names:
            return {}

        src = self._METADATA_SOURCE
        escaped = ", ".join(
            f"'{g.replace(chr(39), chr(39)+chr(39))}'" for g in group_names
        )

        rows = self._run_sql(
            f"SELECT group_name, user_display_name, email "
            f"FROM {src}.users_and_groups "
            f"WHERE group_name IN ({escaped}) "
            f"ORDER BY group_name, user_display_name",
            quiet=True,
        )

        members: dict[str, list[dict]] = {}
        if rows:
            for row in rows:
                gname = row[0]
                if gname not in members:
                    members[gname] = []
                members[gname].append({
                    "name": row[1] or "",
                    "email": row[2] or "",
                })
        return members

scanner = CatalogScanner()
