
import asyncio
import sys

# Playwright on Windows requires ProactorEventLoop
# because it launches browser subprocesses internally.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
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
from app.payments.routes import router as payments_router
from app.features.routes import router as features_router

# Absolute path to this file's directory (app/)
# Prevents broken paths when the working directory differs from the project root
BASE_DIR = Path(__file__).resolve().parent
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


def _csp_connect_src() -> str:
    sources = ["'self'", "https://api.paystack.co"]
    if settings.push_api_base_url:
        parsed = urlparse(settings.push_api_base_url)
        if parsed.scheme and parsed.netloc:
            sources.append(f"{parsed.scheme}://{parsed.netloc}")
    return " ".join(sources)


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
            f"connect-src {_csp_connect_src()}; "
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


@app.get("/service-worker.js", include_in_schema=False)
async def service_worker():
    return FileResponse(
        str(PROJECT_ROOT / "public" / "service-worker.js"),
        media_type="application/javascript",
    )


@app.get("/push-subscription-manager.js", include_in_schema=False)
async def push_subscription_manager():
    return FileResponse(
        str(PROJECT_ROOT / "public" / "push-subscription-manager.js"),
        media_type="application/javascript",
    )


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    return FileResponse(
        str(BASE_DIR / "pwa" / "manifest.json"),
        media_type="application/manifest+json",
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
        {"request": request, "settings": settings},
    )


@app.get("/payment-success", response_class=HTMLResponse)
async def payment_success(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "settings": settings},
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
