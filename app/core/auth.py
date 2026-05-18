from fastapi import Header, HTTPException, status
from app.core.config import settings


async def require_admin(x_admin_token: str = Header(...)):
    """
    Simple token-based admin guard.
    Replace with Clerk/Supabase Auth JWT validation for production SSO.
    """
    if x_admin_token != settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin token.",
        )
