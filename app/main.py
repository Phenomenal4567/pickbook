from contextlib import asynccontextmanager
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
from app.core.database import engine, Base
from app.author_portal.routes import router as author_router
from app.admin.routes import router as admin_router
from app.ingest.routes import router as ingest_router

# =========================================================
# DATABASE INIT — lifespan replaces deprecated @on_event
# =========================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once on startup (before yield) and once on shutdown (after yield).
    Safer than @app.on_event in multi-worker / production deployments.
    """
    Base.metadata.create_all(bind=engine)
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

# =========================================================
# STATIC FILES
# =========================================================
app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static",
)

# =========================================================
# TEMPLATES
# =========================================================
templates = Jinja2Templates(directory="app/templates")

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

# =========================================================
# HOMEPAGE
# =========================================================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request},
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
    return FileResponse("app/static/favicon.ico")

# =========================================================
# DEBUG: DATABASE TABLES  (non-production only)
# =========================================================
if settings.app_env != "production":
    @app.get("/debug-tables", dependencies=[Depends(verify_admin)])
    def debug_tables():
        """Only reachable in dev/staging, and only with a valid admin token."""
        inspector = inspect(engine)
        return {"tables": inspector.get_table_names()}