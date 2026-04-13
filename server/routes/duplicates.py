import logging
import traceback
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from server.scanner import scanner
from server.duplicates import detect_duplicates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


@router.get("/detect")
def detect(threshold: float = Query(0.5, ge=0.1, le=1.0)):
    """Force a fresh duplicate detection (e.g. after threshold change).

    For normal scans, detection runs in the background thread and
    results are available via GET /groups.
    """
    try:
        if not scanner.is_scanned:
            return JSONResponse(
                status_code=400,
                content={"error": "No scan has been run yet"},
            )

        tables = scanner.get_all_tables_raw()
        groups = detect_duplicates(tables, threshold=threshold)
        group_dicts = [g.to_dict() for g in groups]
        scanner.set_duplicate_groups(group_dicts)
        return group_dicts
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "traceback": traceback.format_exc()},
        )


@router.get("/groups")
def get_groups():
    """Return pre-computed duplicate groups (from scan or cache)."""
    return scanner.get_duplicate_groups()
