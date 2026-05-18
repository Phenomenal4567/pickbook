from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    app_env: str = "development"
    secret_key: str = "dev-secret-change-in-production"
    allowed_origins: str = "http://localhost:3000"
    database_url: str = "postgresql://pickbook:pickbook@localhost:5432/pickbook"
    redis_url: str = "redis://localhost:6379/0"
    plausible_domain: str = "pickbook.com"
    admin_token: str = "dev-admin-token"

    @property
    def origins_list(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
