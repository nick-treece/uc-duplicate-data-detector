import logging
import traceback
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from server.scanner import scanner, TableInfo
from server.comparator import compare_tables, fetch_sample_data, build_lineage_context, build_access_tree, compute_shared_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/compare", tags=["compare"])


def _get_table_info(catalog: str, schema: str, table: str) -> TableInfo:
    """Look up a table by name, lazy-loading columns + permissions if needed."""
    if not scanner.is_scanned:
        raise HTTPException(status_code=400, detail="No scan has been run yet")

    t = scanner.get_table_raw(catalog, schema, table)
    if t is None:
        raise HTTPException(
            status_code=404,
            detail=f"Table {catalog}.{schema}.{table} not found in scan results",
        )

    # Lazy-load columns + permissions (cache-restore path)
    if not t.columns:
        t.columns = scanner.load_table_columns(catalog, schema, table)
        scanner.load_table_permissions(catalog, t)

    return t


# ── Sample route MUST come before the 6-segment wildcard route ────────────
@router.get("/sample/{catalog}/{schema}/{table}")
def sample(catalog: str, schema: str, table: str):
    try:
        t = _get_table_info(catalog, schema, table)
        result = fetch_sample_data(t.full_name)
        if result is None:
            raise HTTPException(status_code=500, detail="Could not fetch sample data")
        return result
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "traceback": traceback.format_exc()},
        )


@router.get("/{cat1}/{schema1}/{table1}/{cat2}/{schema2}/{table2}")
def compare(cat1: str, schema1: str, table1: str, cat2: str, schema2: str, table2: str):
    try:
        ta = _get_table_info(cat1, schema1, table1)
        tb = _get_table_info(cat2, schema2, table2)
        result = compare_tables(ta, tb)

        # Attach lineage context if available
        try:
            col_mappings = scanner.query_column_lineage(ta.full_name, tb.full_name)
            lineage_ctx = build_lineage_context(
                table_a_name=ta.full_name,
                table_b_name=tb.full_name,
                upstream_map=scanner._upstream_map,
                downstream_map=scanner._downstream_map,
                consumer_counts=scanner._consumer_counts,
                column_mappings=col_mappings,
                lineage_edges=scanner._lineage_edges,
                transitive_upstream=scanner._transitive_upstream,
            )
            result["lineage"] = lineage_ctx
        except Exception as lineage_err:
            import traceback as tb
            logger.error(f"Lineage context failed: {lineage_err}")
            logger.error(tb.format_exc())
            result["lineage"] = {
                "has_lineage": False,
                "error": str(lineage_err),
            }

        # Build access trees with group membership
        try:
            all_principals = set()
            for t in [ta, tb]:
                if hasattr(t, "permissions") and t.permissions:
                    for p in t.permissions:
                        principal = p.principal if hasattr(p, "principal") else p.get("principal", "")
                        all_principals.add(principal)

            group_members = scanner.query_group_members(list(all_principals))
            result["access_tree_a"] = build_access_tree(ta, group_members)
            result["access_tree_b"] = build_access_tree(tb, group_members)
        except Exception as access_err:
            logger.error(f"Access tree failed: {access_err}")
            result["access_tree_a"] = []
            result["access_tree_b"] = []

        return result
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "traceback": traceback.format_exc()},
        )


@router.get("/debug/lineage-status")
def lineage_status():
    """Debug endpoint: returns scanner lineage state."""
    upstream_size = len(scanner._upstream_map) if hasattr(scanner, '_upstream_map') else -1
    downstream_size = len(scanner._downstream_map) if hasattr(scanner, '_downstream_map') else -1
    edges_size = len(scanner._lineage_edges) if hasattr(scanner, '_lineage_edges') else -1
    consumers_size = len(scanner._consumer_counts) if hasattr(scanner, '_consumer_counts') else -1

    # Sample: first 5 entries from upstream_map
    sample_upstream = {}
    if hasattr(scanner, '_upstream_map') and scanner._upstream_map:
        for k, v in list(scanner._upstream_map.items())[:5]:
            sample_upstream[k] = sorted(v)[:3]

    transitive_size = len(scanner._transitive_upstream) if hasattr(scanner, '_transitive_upstream') else -1
    transitive_entries = sum(len(v) for v in scanner._transitive_upstream.values()) if hasattr(scanner, '_transitive_upstream') and scanner._transitive_upstream else 0

    return {
        "is_scanned": scanner.is_scanned,
        "upstream_map_size": upstream_size,
        "downstream_map_size": downstream_size,
        "lineage_edges_size": edges_size,
        "consumer_counts_size": consumers_size,
        "transitive_upstream_tables": transitive_size,
        "transitive_upstream_entries": transitive_entries,
        "upstream_map_type": type(scanner._upstream_map).__name__ if hasattr(scanner, '_upstream_map') else "missing",
        "sample_upstream": sample_upstream,
    }


@router.get("/debug/access-test/{cat1}/{s1}/{t1}/{cat2}/{s2}/{t2}")
def access_test(cat1: str, s1: str, t1: str, cat2: str, s2: str, t2: str):
    """Debug: returns raw access tree + shared_access for a pair."""
    try:
        ta = _get_table_info(cat1, s1, t1)
        tb = _get_table_info(cat2, s2, t2)

        all_principals = set()
        for t in [ta, tb]:
            if hasattr(t, "permissions") and t.permissions:
                for p in t.permissions:
                    principal = p.principal if hasattr(p, "principal") else p.get("principal", "")
                    all_principals.add(principal)

        group_members = scanner.query_group_members(list(all_principals))
        tree_a = build_access_tree(ta, group_members)
        tree_b = build_access_tree(tb, group_members)
        shared = compute_shared_access(tree_a, tree_b)

        return {
            "principals_queried": sorted(all_principals),
            "group_members_found": {k: len(v) for k, v in group_members.items()},
            "tree_a_summary": [{"p": p["principal"], "type": p["type"], "members": len(p["members"])} for p in tree_a],
            "tree_b_summary": [{"p": p["principal"], "type": p["type"], "members": len(p["members"])} for p in tree_b],
            "shared_access": shared,
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
