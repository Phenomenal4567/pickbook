from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.core.limiter import limiter
from app.core.database import engine, Base
from app.author_portal.routes import router as author_router
from app.admin.routes import router as admin_router
from app.ingest.routes import router as ingest_router



# ─────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent


# ─────────────────────────────────────────────────────────
# MIGRATIONS — add any missing columns to existing tables
# ─────────────────────────────────────────────────────────
def run_migrations():
    """
    Safe, idempotent schema migrations.
    Runs on every startup — only applies changes when the column is missing.
    This handles the case where create_all() cannot add columns to existing tables.
    """
    migrations = [
        "ALTER TABLE books ADD COLUMN IF NOT EXISTS synopsis TEXT",
        "ALTER TABLE books ADD COLUMN IF NOT EXISTS cover TEXT",
        "ALTER TABLE books ADD COLUMN IF NOT EXISTS language VARCHAR(10) DEFAULT 'en'",
        "ALTER TABLE books ADD COLUMN IF NOT EXISTS source VARCHAR(100)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
            except Exception as e:
                print(f"[migration] skipped: {sql[:60]}… — {e}")
        conn.commit()
    print("[migration] schema up to date")


# ─────────────────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create any brand-new tables
    Base.metadata.create_all(bind=engine)
    # 2. Add missing columns to existing tables
    run_migrations()
    yield


# ─────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────
app = FastAPI(
    title="PickBook",
    version="5.1.0",
    lifespan=lifespan,
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None if settings.app_env == "production" else "/redoc",
)


# ─────────────────────────────────────────────────────────
# RATE LIMITING
# ─────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ─────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────
# STATIC FILES
# ─────────────────────────────────────────────────────────
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)


# ─────────────────────────────────────────────────────────
# TEMPLATES
# ─────────────────────────────────────────────────────────
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ─────────────────────────────────────────────────────────
# ADMIN DEPENDENCY
# ─────────────────────────────────────────────────────────
def verify_admin(x_admin_token: str = Header(None)) -> None:
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid or missing admin token")


# ─────────────────────────────────────────────────────────
# ROUTERS
# ─────────────────────────────────────────────────────────
app.include_router(author_router)
app.include_router(admin_router, dependencies=[Depends(verify_admin)])
app.include_router(ingest_router)


# ─────────────────────────────────────────────────────────
# HOMEPAGE
# ─────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ─────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": "PickBook",
        "version": "5.1.0",
        "env": settings.app_env,
    }


# ─────────────────────────────────────────────────────────
# FAVICON
# ─────────────────────────────────────────────────────────
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(str(BASE_DIR / "static" / "favicon.ico"))


# ─────────────────────────────────────────────────────────
# GLOBAL ERROR HANDLER — prevents raw 500s leaking to users
# ─────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"[error] unhandled exception on {request.url}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Please try again later."},
    )


# ─────────────────────────────────────────────────────────
# DEBUG ROUTES (dev / staging only)
# ─────────────────────────────────────────────────────────
if settings.app_env != "production":

    @app.get("/debug-tables", dependencies=[Depends(verify_admin)])
    def debug_tables():
        inspector = inspect(engine)
        return {"tables": inspector.get_table_names()}

    @app.get("/debug-columns", dependencies=[Depends(verify_admin)])
    def debug_columns():
        inspector = inspect(engine)
        return {
            table: [c["name"] for c in inspector.get_columns(table)]
            for table in inspector.get_table_names()
        }

        