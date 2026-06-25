import logging
import traceback
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from server.scanner import scanner
from server.duplicates import detect_duplicates, detect_schema_duplicates

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
    """Return pairs of similar schemas across different catalogs."""
    if not scanner.is_scanned:
        return JSONResponse(status_code=400, content={"error": "No scan has been run yet"})
    try:
        results = detect_schema_duplicates(scanner._tables, threshold=threshold)
        return {"groups": results, "total": len(results), "threshold": threshold}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e), "traceback": traceback.format_exc()})
