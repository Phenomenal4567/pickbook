import os
from slowapi import Limiter
from slowapi.util import get_remote_address

def _rate_limit_storage_uri() -> str:
    """Use Redis for rate limiting only when it is configured explicitly."""
    explicit_uri = os.getenv("RATELIMIT_STORAGE_URI", "").strip()
    if explicit_uri:
        return explicit_uri

    # Railway can expose REDIS_URL for unrelated services. If those credentials
    # are stale or disabled, SlowAPI raises before the endpoint can run.
    return "memory://"

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_rate_limit_storage_uri()
)
