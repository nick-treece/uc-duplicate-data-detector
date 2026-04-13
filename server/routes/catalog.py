import traceback
import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from server.scanner import scanner
from server.cache import CacheManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/catalog", tags=["catalog"])

# Shared cache manager instance
cache_manager = CacheManager(scanner)


@router.get("/list")
def list_catalogs():
    try:
        scanner.reset_client()
        return scanner.list_catalogs()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "traceback": traceback.format_exc()},
        )


@router.post("/scan-all")
def scan_all():
    """Start a background scan. Returns immediately with status."""
    try:
        return scanner.start_scan()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "traceback": traceback.format_exc()},
        )


@router.get("/scan-status")
def scan_status():
    """Poll the current scan progress."""
    return scanner.get_scan_status()


# ── Cache endpoints ───────────────────────────────────────────────────────

@router.get("/cache-status")
def cache_status():
    """Check whether a valid cache exists."""
    try:
        return cache_manager.get_cache_status()
    except Exception as e:
        logger.warning(f"Cache status check failed: {e}")
        return {"valid": False, "reason": f"Error checking cache: {e}"}


@router.post("/cache-load")
def cache_load():
    """Load scan results from cache + bulk-query metadata.

    Returns the cached scan_result and duplicate groups so the
    frontend can restore state without a full scan.
    """
    try:
        # Validate cache first
        status = cache_manager.get_cache_status()
        if not status.get("valid"):
            return JSONResponse(
                status_code=409,
                content={"error": "Cache is not valid", "detail": status},
            )

        # Load scan result summary from cache
        scan_result = cache_manager.load_scan_result()
        if not scan_result:
            return JSONResponse(
                status_code=404,
                content={"error": "No cached scan result found"},
            )

        # Load duplicate groups from cache
        groups = cache_manager.load_groups()

        # Bulk-load tables + schemas from metadata (fast: 2 queries)
        scanner.load_from_cache(scan_result)

        # Store groups so /api/duplicates/groups can serve them
        scanner.set_duplicate_groups(groups)

        return {
            "scan_result": scan_result,
            "groups": groups,
            "cache_age_days": status.get("age_days"),
            "cached_at": status.get("cached_at"),
        }
    except Exception as e:
        logger.exception("Cache load failed")
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "traceback": traceback.format_exc()},
        )


@router.get("/schemas")
def list_schemas(catalog: str | None = Query(None)):
    if not scanner.is_scanned:
        return []
    return scanner.get_schemas(catalog)


@router.get("/tables")
def list_tables(schema: str | None = None, catalog: str | None = Query(None)):
    if not scanner.is_scanned:
        return []
    return scanner.get_tables(schema, catalog)


@router.get("/table/{catalog}/{schema}/{table}")
def get_table(catalog: str, schema: str, table: str):
    if not scanner.is_scanned:
        raise HTTPException(status_code=400, detail="No scan has been run yet")
    result = scanner.get_table_by_full_name(catalog, schema, table)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Table {catalog}.{schema}.{table} not found")
    return result
