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


def build_lineage_context(
    table_a_name: str,
    table_b_name: str,
    upstream_map: dict,
    downstream_map: dict,
    consumer_counts: dict,
    column_mappings: list,
    lineage_edges: list,
) -> dict:
    """Build lineage context for the compare page between two tables."""
    a = table_a_name.lower()
    b = table_b_name.lower()

    # Direct flow between the two tables
    a_downstream = downstream_map.get(a, set())
    b_downstream = downstream_map.get(b, set())
    direct_flow = None
    if b in a_downstream:
        direct_flow = {"direction": "a_to_b", "source": table_a_name, "target": table_b_name}
    elif a in b_downstream:
        direct_flow = {"direction": "b_to_a", "source": table_b_name, "target": table_a_name}

    # Entity types for the direct flow
    flow_entity_types = []
    if direct_flow:
        for edge in lineage_edges:
            if edge.source_table == a and edge.target_table == b:
                flow_entity_types = sorted(edge.entity_types)
                break
            elif edge.source_table == b and edge.target_table == a:
                flow_entity_types = sorted(edge.entity_types)
                break
        # Drop "UNKNOWN" when real entity types are present
        if len(flow_entity_types) > 1:
            flow_entity_types = [t for t in flow_entity_types if t != "UNKNOWN"]
        direct_flow["entity_types"] = flow_entity_types

    # Shared upstream sources
    a_upstream = upstream_map.get(a, set())
    b_upstream = upstream_map.get(b, set())
    shared_upstream = sorted(a_upstream & b_upstream)
    shared_downstream = sorted(a_downstream & b_downstream)

    # Consumer counts
    a_consumers = consumer_counts.get(a, 0)
    b_consumers = consumer_counts.get(b, 0)

    # Column-level mappings (pre-queried on demand for this pair)
    col_mappings = column_mappings or []

    has_lineage = bool(
        direct_flow or shared_upstream or a_consumers or b_consumers or col_mappings
    )

    # Per-table lineage profiles (for side-by-side comparison)
    upstream_a = sorted(a_upstream)
    upstream_b = sorted(b_upstream)
    downstream_a = sorted(a_downstream)
    downstream_b = sorted(b_downstream)

    has_lineage = bool(
        direct_flow or shared_upstream
        or a_consumers or b_consumers
        or col_mappings
        or upstream_a or upstream_b
        or downstream_a or downstream_b
    )

    return {
        "has_lineage": has_lineage,
        "direct_flow": direct_flow,
        "shared_upstream": shared_upstream,
        "consumer_counts": {"a": a_consumers, "b": b_consumers},
        "column_mappings": col_mappings,
        "upstream_a": upstream_a,
        "upstream_b": upstream_b,
        "downstream_a": downstream_a,
        "downstream_b": downstream_b,
        "shared_downstream": shared_downstream,
    }


def build_access_tree(table_info, group_members: dict) -> list[dict]:
    """Build an access tree for a table's permissions.

    Returns a list of principal entries, each with:
      - principal: group or user name
      - privileges: list of privilege strings
      - type: "group" or "user"
      - members: list of {name, email} if type is "group"
    """
    tree = []
    if not hasattr(table_info, "permissions") or not table_info.permissions:
        return tree

    for perm in table_info.permissions:
        principal = perm.principal if hasattr(perm, "principal") else perm.get("principal", "")
        privs = perm.privileges if hasattr(perm, "privileges") else perm.get("privileges", [])

        members = group_members.get(principal, [])
        is_group = len(members) > 0

        tree.append({
            "principal": principal,
            "privileges": privs,
            "type": "group" if is_group else "user",
            "members": members,
        })

    # Sort: groups first, then users
    tree.sort(key=lambda x: (0 if x["type"] == "group" else 1, x["principal"]))
    return tree



def compute_shared_access(tree_a: list[dict], tree_b: list[dict]) -> dict:
    """Identify shared principals between two access trees.

    Returns:
      - shared_groups: groups that appear in both trees
      - shared_users: individual users in shared groups (union of members)
      - only_a_groups / only_b_groups: groups unique to each table
    """
    groups_a = {p["principal"] for p in tree_a if p["type"] == "group"}
    groups_b = {p["principal"] for p in tree_b if p["type"] == "group"}

    shared = sorted(groups_a & groups_b)
    only_a = sorted(groups_a - groups_b)
    only_b = sorted(groups_b - groups_a)

    # Users who appear in shared groups
    members_a = {}
    members_b = {}
    for p in tree_a:
        if p["type"] == "group":
            members_a[p["principal"]] = {m["email"] for m in p["members"]}
    for p in tree_b:
        if p["type"] == "group":
            members_b[p["principal"]] = {m["email"] for m in p["members"]}

    shared_users = set()
    for g in shared:
        shared_users |= members_a.get(g, set()) & members_b.get(g, set())

    return {
        "shared_groups": shared,
        "only_a_groups": only_a,
        "only_b_groups": only_b,
        "shared_user_count": len(shared_users),
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
