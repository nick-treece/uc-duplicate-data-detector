"""Table comparison logic: column diff, type diff, permissions diff, sample data."""

from __future__ import annotations
import json
import logging
import urllib.request
from typing import Optional

from server.config import get_workspace_client, WAREHOUSE_ID
from server.scanner import TableInfo

logger = logging.getLogger(__name__)


def compare_tables(table_a: TableInfo, table_b: TableInfo) -> dict:
    cols_a = {c.name.lower(): c for c in table_a.columns}
    cols_b = {c.name.lower(): c for c in table_b.columns}

    def _tstr(t):
        """Convert type_name (which may be an enum) to string."""
        if hasattr(t, 'value'):
            return str(t.value)
        return str(t)

    all_cols = sorted(set(cols_a.keys()) | set(cols_b.keys()))
    shared = set(cols_a.keys()) & set(cols_b.keys())
    only_a = set(cols_a.keys()) - set(cols_b.keys())
    only_b = set(cols_b.keys()) - set(cols_a.keys())

    column_diff = []
    for col in all_cols:
        in_a = col in cols_a
        in_b = col in cols_b
        status = "shared"
        type_match = True
        if not in_a:
            status = "only_b"
        elif not in_b:
            status = "only_a"
        else:
            type_a = _tstr(cols_a[col].type_name).lower()
            type_b = _tstr(cols_b[col].type_name).lower()
            type_match = type_a == type_b

        column_diff.append({
            "column": col,
            "status": status,
            "type_a": _tstr(cols_a[col].type_name) if in_a else None,
            "type_b": _tstr(cols_b[col].type_name) if in_b else None,
            "type_match": type_match if status == "shared" else None,
        })

    # Permissions comparison
    perms_a = {}
    perms_b = {}
    if hasattr(table_a, 'permissions') and table_a.permissions:
        for p in table_a.permissions:
            principal = p.principal if hasattr(p, 'principal') else p.get('principal', '')
            privs = p.privileges if hasattr(p, 'privileges') else p.get('privileges', [])
            perms_a[principal] = privs
    if hasattr(table_b, 'permissions') and table_b.permissions:
        for p in table_b.permissions:
            principal = p.principal if hasattr(p, 'principal') else p.get('principal', '')
            privs = p.privileges if hasattr(p, 'privileges') else p.get('privileges', [])
            perms_b[principal] = privs
    all_principals = sorted(set(list(perms_a.keys()) + list(perms_b.keys())))
    permissions_diff = []
    for principal in all_principals:
        p_a = perms_a.get(principal, [])
        p_b = perms_b.get(principal, [])
        permissions_diff.append({
            "principal": principal,
            "privileges_a": p_a,
            "privileges_b": p_b,
            "match": set(p_a) == set(p_b) if p_a and p_b else False,
        })

    return {
        "table_a": {
            "full_name": table_a.full_name,
            "schema": table_a.schema,
            "name": table_a.name,
            "column_count": len(table_a.columns),
            "owner": table_a.owner,
            "comment": table_a.comment,
            "updated_at": table_a.updated_at,
        },
        "table_b": {
            "full_name": table_b.full_name,
            "schema": table_b.schema,
            "name": table_b.name,
            "column_count": len(table_b.columns),
            "owner": table_b.owner,
            "comment": table_b.comment,
            "updated_at": table_b.updated_at,
        },
        "column_diff": column_diff,
        "shared_columns": len(shared),
        "only_a_columns": len(only_a),
        "only_b_columns": len(only_b),
        "permissions_diff": permissions_diff,
    }


def fetch_sample_data(full_name: str, limit: int = 10) -> Optional[dict]:
    """Fetch sample rows via SQL warehouse using the SDK's API client."""
    try:
        client = get_workspace_client()
        sql = f"SELECT * FROM {full_name} LIMIT {limit}"

        raw = client.api_client.do(
            "POST",
            "/api/2.0/sql/statements/",
            body={
                "statement": sql,
                "warehouse_id": WAREHOUSE_ID,
                "wait_timeout": "30s",
                "format": "JSON_ARRAY",
            },
        )
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw

        if data.get("status", {}).get("state") != "SUCCEEDED":
            logger.warning(f"SQL query failed for {full_name}: {data.get('status', {})}")
            return None

        columns = []
        schema = data.get("manifest", {}).get("schema", {})
        for col in schema.get("columns", []):
            columns.append({"name": col["name"], "type": col.get("type_name", "")})

        rows = data.get("result", {}).get("data_array", [])
        return {"columns": columns, "rows": rows}
    except Exception as e:
        logger.warning(f"Sample data fetch failed for {full_name}: {e}")
        return None
