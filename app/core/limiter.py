import os
from slowapi import Limiter
from slowapi.util import get_remote_address

# 1. Look for Railway's default Redis variable, or a custom one. 
# 2. Fallback to "memory://" if neither exists (great for local dev).
redis_url = os.getenv("REDIS_URL") or os.getenv("RATELIMIT_STORAGE_URI") or "memory://"

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=redis_url
)