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

    # Scraper feature flags
    use_playwright: bool = False
    nf_enrich_details: bool = False  # legacy flag — kept for .env compatibility
    max_chapters_per_book: int = 50
    max_cached_chapter_bytes: int = 2_000_000
    initial_chapters_per_book: int = 2
    anystories_pages_per_genre: int = 3

    @property
    def origins_list(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
