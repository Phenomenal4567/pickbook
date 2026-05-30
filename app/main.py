
import asyncio
import sys
from urllib.parse import urlparse

# Playwright on Windows requires ProactorEventLoop
# because it launches browser subprocesses internally.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from contextlib import asynccontextmanager
from pathlib import Path
import requests
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.core.config import settings
from app.core.limiter import limiter
from app.core.database import engine, initialize_database
from app.author_portal.routes import router as author_router
from app.admin.routes import router as admin_router
from app.ingest.routes import router as ingest_router
from app.payments.routes import router as payments_router
from app.features.routes import router as features_router

# Absolute path to this file's directory (app/)
# Prevents broken paths when the working directory differs from the project root
BASE_DIR = Path(__file__).resolve().parent
IMAGE_PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
IMAGE_PROXY_HOSTS = {
    "freewebnovel.com",
    "www.freewebnovel.com",
    "lightnovelworld.org",
    "www.lightnovelworld.org",
    "royalroad.com",
    "www.royalroad.com",
}

# =========================================================
# DATABASE INIT — lifespan replaces deprecated @on_event
# =========================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once on startup (before yield) and once on shutdown (after yield).
    Safer than @app.on_event in multi-worker / production deployments.
    """
    initialize_database()
    yield
    # Add any teardown logic here (e.g. close connection pools)

# =========================================================
# FASTAPI APP
# =========================================================
app = FastAPI(
    title="PickBook Launch Platform",
    version="5.0.0",
    lifespan=lifespan,
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None if settings.app_env == "production" else "/redoc",
)

# =========================================================
# RATE LIMITING
# =========================================================
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# =========================================================
# CORS
# =========================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://api.paystack.co; "
            "frame-src https://checkout.paystack.com; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        ),
    )
    return response

# =========================================================
# STATIC FILES — absolute path avoids working-directory issues
# =========================================================
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)

PROJECT_ROOT = BASE_DIR.parent
(PROJECT_ROOT / "public" / "uploads").mkdir(parents=True, exist_ok=True)
app.mount(
    "/uploads",
    StaticFiles(directory=str(PROJECT_ROOT / "public" / "uploads")),
    name="uploads",
)

# =========================================================
# TEMPLATES — absolute path avoids working-directory issues
# =========================================================
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# =========================================================
# ADMIN SECURITY DEPENDENCY
# =========================================================
def verify_admin(x_admin_token: str = Header(None)) -> None:
    """
    Inject via Depends(verify_admin) on any route or router that
    requires admin privileges. Raises 403 on missing / wrong token.
    """
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing admin token",
        )

# =========================================================
# ROUTERS
# =========================================================
app.include_router(author_router)
# admin_router is protected globally — every route requires a valid token
app.include_router(admin_router, dependencies=[Depends(verify_admin)])
app.include_router(ingest_router)
app.include_router(payments_router)
app.include_router(features_router)


@app.get("/admin/tools", response_class=HTMLResponse)
async def public_admin_tools(request: Request):
    return templates.TemplateResponse(
        "admin_tools.html",
        {"request": request},
    )

# =========================================================
# HOMEPAGE
# =========================================================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request},
    )


@app.get("/payment-success", response_class=HTMLResponse)
async def payment_success(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request},
    )


@app.get("/cover-proxy")
def cover_proxy(url: str):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if parsed.scheme not in {"http", "https"} or host not in IMAGE_PROXY_HOSTS:
        raise HTTPException(status_code=400, detail="Unsupported cover URL")

    try:
        response = requests.get(
            url,
            headers=IMAGE_PROXY_HEADERS,
            timeout=20,
            stream=True,
        )
        response.raise_for_status()
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Cover image unavailable")

    content_type = response.headers.get("content-type", "image/jpeg")
    if not content_type.startswith("image/"):
        response.close()
        raise HTTPException(status_code=502, detail="Cover URL is not an image")

    return StreamingResponse(
        response.iter_content(chunk_size=8192),
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )

# =========================================================
# HEALTH CHECK
# =========================================================
@app.get("/health")
def health():
    return {
        "health": "ok",
        "app": "PickBook Launch Platform",
        "version": "5.0.0",
    }

# =========================================================
# FAVICON
# =========================================================
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(str(BASE_DIR / "static" / "favicon.ico"))

# =========================================================
# DEBUG: DATABASE TABLES  (non-production only)
# =========================================================
if settings.app_env != "production":
    @app.get("/debug-tables", dependencies=[Depends(verify_admin)])
    def debug_tables():
        """Only reachable in dev/staging, and only with a valid admin token."""
        inspector = inspect(engine)
        return {"tables": inspector.get_table_names()}
