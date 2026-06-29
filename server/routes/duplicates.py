import logging
import traceback
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.scanner import scanner
from server.duplicates import detect_duplicates
from server.routes.catalog import cache_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


class DismissRequest(BaseModel):
    group_key: str
    group_type: str   # 'object' | 'schema'
    rationale: str


@router.post("/detect")
def detect(threshold: float = Query(0.5, ge=0.1, le=1.0)):
    """Start a background re-detection with a new threshold."""
    if not scanner.is_scanned:
        return JSONResponse(status_code=400, content={"error": "No scan has been run yet"})
    return scanner.start_detection(threshold)


@router.get("/detect-status")
def detect_status():
    return scanner.get_scan_status()


@router.get("/groups")
def get_groups():
    return scanner.get_duplicate_groups()


@router.get("/schema-groups")
def schema_groups(threshold: float = Query(0.7, ge=0.1, le=1.0)):
    """Return pre-computed schema duplicate groups (cached at scan time)."""
    if not scanner.is_scanned:
        return JSONResponse(status_code=400, content={"error": "No scan has been run yet"})
    all_groups = scanner.get_schema_groups()
    filtered = [g for g in all_groups if g.get("max_table_similarity", 0) >= threshold]
    return {"groups": filtered, "total": len(filtered), "threshold": threshold}


# ── Dismiss endpoints ──────────────────────────────────────────────────────

@router.get("/dismissed")
def get_dismissed():
    """Return all persisted dismissal records."""
    return cache_manager.load_dismissed_groups()


@router.post("/dismiss")
def dismiss_group(req: DismissRequest):
    """Persist a group dismissal with rationale."""
    cache_manager.save_dismissed_group(req.group_key, req.group_type, req.rationale)
    return {"status": "ok"}


@router.delete("/dismiss/{group_key:path}")
def undismiss_group(group_key: str):
    """Remove a dismissal record."""
    cache_manager.delete_dismissed_group(group_key)
    return {"status": "ok"}
