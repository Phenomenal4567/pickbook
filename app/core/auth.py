from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.book import Profile

bearer_scheme = HTTPBearer(auto_error=False)


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


def _is_standard_profile(profile: Profile) -> bool:
    if profile.current_plan != "standard":
        return False

    if not profile.subscription_expiry:
        return True

    expiry = profile.subscription_expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    return expiry > datetime.now(timezone.utc)


def get_current_profile(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_user_id: str | None = Header(None),
):
    """
    Temporary local profile resolver.

    The PRD targets Supabase JWTs. Until that migration lands, this accepts
    either Authorization: Bearer <profile-id> or X-User-Id for local testing.
    """
    token = credentials.credentials if credentials else None
    profile_id = token or x_user_id

    if not profile_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in required.",
        )

    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == profile_id).first()
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Profile not found.",
            )
        return profile
    finally:
        db.close()


def require_standard_profile(profile: Profile = Depends(get_current_profile)):
    if not _is_standard_profile(profile):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Standard plan required.",
        )
    return profile
