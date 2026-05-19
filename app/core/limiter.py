from slowapi import Limiter
from slowapi.util import get_remote_address
from app.core.config import settings

# Centrally configured rate limiter used across all routers.
# storage_uri must point at Redis so that rate-limit counters are shared
# across all uvicorn workers. Without this, each worker keeps its own
# in-memory counter and users can exceed the limit by hitting different
# workers (N workers × limit requests per window).
limiter = Limiter(key_func=get_remote_address, storage_uri=settings.redis_url)
