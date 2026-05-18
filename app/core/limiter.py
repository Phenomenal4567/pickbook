from slowapi import Limiter
from slowapi.util import get_remote_address

# Centrally configured rate limiter used across all routers
limiter = Limiter(key_func=get_remote_address)
