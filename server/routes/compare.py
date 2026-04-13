import traceback
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from server.scanner import scanner, TableInfo
from server.comparator import compare_tables, fetch_sample_data

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
        return compare_tables(ta, tb)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "traceback": traceback.format_exc()},
        )
