from fastapi import APIRouter, Request
from app.core.limiter import limiter

# Auth is applied globally in main.py via Depends(verify_admin) on include_router.
# Do NOT add a second auth dependency here — it causes double-checking and
# inconsistent error codes (401 vs 403) depending on which guard fires first.
router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
)


@router.get("/dashboard")
@limiter.limit("60/minute")
def dashboard(request: Request):
    return {
        "system_status": "healthy",
        "pending_reviews": 12,
        "quarantined_books": 3,
        "approved_today": 42,
        "active_users": 1280,
    }
