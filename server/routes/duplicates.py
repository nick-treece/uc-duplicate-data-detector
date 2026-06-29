import logging
import traceback
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from server.scanner import scanner
from server.duplicates import detect_duplicates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


@router.post("/detect")
def detect(threshold: float = Query(0.5, ge=0.1, le=1.0)):
    """Start a background re-detection with a new threshold.

    Returns immediately.  Poll GET /detect-status for progress,
    then GET /groups for results.
    """
    if not scanner.is_scanned:
        return JSONResponse(
            status_code=400,
            content={"error": "No scan has been run yet"},
        )

    return scanner.start_detection(threshold)


@router.get("/detect-status")
def detect_status():
    """Poll detection progress (reuses scan status infrastructure)."""
    return scanner.get_scan_status()


@router.get("/groups")
def get_groups():
    """Return pre-computed duplicate groups (from scan or cache)."""
    return scanner.get_duplicate_groups()


@router.get("/schema-groups")
def schema_groups(threshold: float = Query(0.7, ge=0.1, le=1.0)):
    """Return pre-computed schema duplicate groups (cached at scan time).

    Results are computed at threshold=0.7 during the scan and cached.
    The threshold parameter filters the cached results in-memory by
    max_table_similarity; values below 0.7 return all cached groups.
    """
    if not scanner.is_scanned:
        return JSONResponse(status_code=400, content={"error": "No scan has been run yet"})
    all_groups = scanner.get_schema_groups()
    filtered = [g for g in all_groups if g.get("max_table_similarity", 0) >= threshold]
    return {"groups": filtered, "total": len(filtered), "threshold": threshold}
