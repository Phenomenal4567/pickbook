from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.book import Profile

bearer_scheme = HTTPBearer(auto_error=False)
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15


def create_access_token(profile_id: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(
        {
            "sub": profile_id,
            "exp": expires_at,
            "typ": "access",
        },
        settings.secret_key,
        algorithm=JWT_ALGORITHM,
    )


def profile_id_from_token(token: str | None) -> str | None:
    if not token:
        return None

    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[JWT_ALGORITHM],
        )
    except JWTError:
        if settings.app_env != "production":
            return token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
        )

    if payload.get("typ") != "access" or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token.",
        )

    return str(payload["sub"])


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
    profile_id = profile_id_from_token(token) or (
        x_user_id if settings.app_env != "production" else None
    )

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
