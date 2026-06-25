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
    transitive_upstream: dict | None = None,
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

    # Deep shared ancestors (transitive)
    shared_ancestors = []
    trans = transitive_upstream or {}
    a_ancestors = trans.get(a, {})
    b_ancestors = trans.get(b, {})
    shared_ancestor_keys = set(a_ancestors.keys()) & set(b_ancestors.keys())
    for anc in sorted(shared_ancestor_keys, key=lambda k: min(a_ancestors[k], b_ancestors[k])):
        shared_ancestors.append({
            "name": anc,
            "depth_a": a_ancestors[anc],
            "depth_b": b_ancestors[anc],
        })

    has_lineage = bool(
        direct_flow or shared_upstream or shared_ancestors
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
        "shared_ancestors": shared_ancestors,
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


def compute_lineage_graph(
    table_a_name: str,
    table_b_name: str,
    upstream_map: dict[str, set[str]],
    downstream_map: dict[str, set[str]],
    consumer_counts: dict[str, int],
    lineage_edges: list,
    max_depth: int = 5,
    max_nodes: int = 100,
) -> dict:
    """Compute the connecting subgraph between two tables.

    BFS upstream from both tables, find shared ancestors, then prune
    branches that don't connect to a shared ancestor. Returns a DAG
    suitable for D3-dagre rendering.

    Returns:
        {
            "nodes": [{"id", "label", "tier", "is_target", "is_shared_ancestor", "consumers"}],
            "edges": [{"source", "target", "entity_types"}],
        }
    """
    from collections import deque

    a = table_a_name.lower()
    b = table_b_name.lower()

    # Step 1: BFS upstream from both tables, recording depth and parents
    def bfs_upstream(start: str) -> dict[str, int]:
        """Returns {node: min_depth} for all nodes reachable upstream."""
        visited = {start: 0}
        queue = deque([(start, 0)])
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for parent in upstream_map.get(node, set()):
                if parent not in visited:
                    visited[parent] = depth + 1
                    queue.append((parent, depth + 1))
        return visited

    reachable_a = bfs_upstream(a)
    reachable_b = bfs_upstream(b)

    # Step 2: Find shared ancestors (nodes reachable from both)
    shared_ancestors = set(reachable_a.keys()) & set(reachable_b.keys())
    shared_ancestors.discard(a)
    shared_ancestors.discard(b)

    if not shared_ancestors and a not in reachable_b and b not in reachable_a:
        # No connecting path exists
        return {"nodes": [], "edges": []}

    # Step 3: Build the connecting subgraph
    # Include all nodes and edges on upstream paths from both targets.
    # This gives a full picture of how the tables connect through their lineage.

    subgraph_nodes: set[str] = {a, b}
    subgraph_edges: set[tuple[str, str]] = set()

    # Add direct edge if exists
    if b in downstream_map.get(a, set()):
        subgraph_edges.add((a, b))
    if a in downstream_map.get(b, set()):
        subgraph_edges.add((b, a))

    # Walk upstream from both tables, collecting all edges on connecting paths
    # A node is "connecting" if it's reachable from both tables (i.e., in both BFS sets)
    for node in reachable_a:
        for parent in upstream_map.get(node, set()):
            if parent in reachable_a:
                # Only include if this edge is on a path to a shared ancestor
                if node in shared_ancestors or parent in shared_ancestors or node == a:
                    subgraph_nodes.add(node)
                    subgraph_nodes.add(parent)
                    subgraph_edges.add((parent, node))

    for node in reachable_b:
        for parent in upstream_map.get(node, set()):
            if parent in reachable_b:
                if node in shared_ancestors or parent in shared_ancestors or node == b:
                    subgraph_nodes.add(node)
                    subgraph_nodes.add(parent)
                    subgraph_edges.add((parent, node))

    # Also include intermediate nodes on shortest paths from targets to shared ancestors
    for target, reachable in [(a, reachable_a), (b, reachable_b)]:
        # Walk from target up to shared ancestors, keeping the chain
        visited = {target}
        queue = deque([target])
        while queue:
            node = queue.popleft()
            if node in shared_ancestors and node != target:
                continue  # stop at shared ancestor
            for parent in upstream_map.get(node, set()):
                if parent in reachable:
                    subgraph_nodes.add(parent)
                    subgraph_nodes.add(node)
                    subgraph_edges.add((parent, node))
                    if parent not in visited:
                        visited.add(parent)
                        queue.append(parent)

    # Step 5: Cap at max_nodes (keep closest nodes to targets)
    if len(subgraph_nodes) > max_nodes:
        # Prioritise: targets first, then by min distance to either target
        def node_priority(n):
            if n == a or n == b:
                return 0
            da = reachable_a.get(n, 999)
            db = reachable_b.get(n, 999)
            return min(da, db)

        sorted_nodes = sorted(subgraph_nodes, key=node_priority)
        subgraph_nodes = set(sorted_nodes[:max_nodes])

    # Step 6: Filter edges to only include those between subgraph nodes
    final_edges = [
        (src, tgt) for src, tgt in subgraph_edges
        if src in subgraph_nodes and tgt in subgraph_nodes
    ]

    # Step 7: Build entity_type lookup from lineage_edges
    edge_entity_types: dict[tuple[str, str], list[str]] = {}
    for edge in lineage_edges:
        key = (edge.source_table, edge.target_table)
        if key[0] in subgraph_nodes and key[1] in subgraph_nodes:
            edge_entity_types[key] = sorted(edge.entity_types - {"UNKNOWN"}) or ["UNKNOWN"]

    # Step 8: Determine medallion tier from catalog name
    def get_tier(node_name: str) -> str:
        parts = node_name.split(".")
        if not parts:
            return "unknown"
        cat = parts[0].lower()
        if "gold" in cat or "10_gold" in cat:
            return "gold"
        if "silver" in cat or "20_silver" in cat:
            return "silver"
        if "bronze" in cat or "30_bronze" in cat:
            return "bronze"
        if "copper" in cat or "40_copper" in cat:
            return "copper"
        return "unknown"

    def short_label(full_name: str) -> str:
        """Short display label: schema.table (omit catalog)."""
        parts = full_name.split(".")
        if len(parts) == 3:
            return f"{parts[1]}.{parts[2]}"
        return full_name

    # Build response
    nodes = []
    for node in sorted(subgraph_nodes):
        nodes.append({
            "id": node,
            "label": short_label(node),
            "tier": get_tier(node),
            "is_target": node == a or node == b,
            "is_shared_ancestor": node in shared_ancestors,
            "consumers": consumer_counts.get(node, 0),
            "depth_from_a": reachable_a.get(node),
            "depth_from_b": reachable_b.get(node),
        })

    edges = []
    for src, tgt in final_edges:
        edges.append({
            "source": src,
            "target": tgt,
            "entity_types": edge_entity_types.get((src, tgt), []),
        })

    return {"nodes": nodes, "edges": edges}
