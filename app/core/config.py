import os

from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    app_env: str = "development"
    secret_key: str = "dev-secret-change-in-production"
    allowed_origins: str = "http://localhost:3000"
    database_url: str = "postgresql://pickbook:pickbook@localhost:5432/pickbook"
    database_sslmode: str = "require"
    database_connect_timeout_seconds: int = 10
    database_startup_retries: int = 36
    database_startup_retry_seconds: int = 5
    local_database_fallback: bool = False
    redis_url: str = "redis://localhost:6379/0"
    plausible_domain: str = "pickbook.com"
    admin_token: str = "dev-admin-token"
    app_base_url: str = "http://127.0.0.1:8000"
    paystack_secret_key: str = ""
    paystack_public_key: str = ""
    paystack_callback_url: str = ""
    standard_plan_price_kobo: int = 250000
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    storage_backend: str = "local"
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_public_bucket: str = "pickbook-public"
    supabase_private_bucket: str = "pickbook-private"

    # Scraper feature flags
    use_playwright: bool = False
    nf_enrich_details: bool = False  # legacy flag — kept for .env compatibility
    max_chapters_per_book: int = 10
    max_cached_chapter_bytes: int = 250_000
    initial_chapters_per_book: int = 0
    anystories_pages_per_genre: int = 3

    @property
    def origins_list(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        if self.database_url.startswith("postgres://"):
            return self.database_url.replace("postgres://", "postgresql+psycopg://", 1)
        return self.database_url

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
import os 
print("DATABASE_URL from os.environ:")
print(os.environ.get("DATABASE_URL"))
