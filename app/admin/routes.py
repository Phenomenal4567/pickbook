from fastapi import APIRouter, Depends, Request
from app.core.auth import require_admin
from app.core.limiter import limiter

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    # All admin routes require a valid admin token
    dependencies=[Depends(require_admin)],
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
