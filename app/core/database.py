from pathlib import Path
from time import sleep

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings


def _connect_args_for(database_url: str) -> dict:
    if database_url.startswith("postgres"):
        return {
            "sslmode": settings.database_sslmode,
            "connect_timeout": settings.database_connect_timeout_seconds,
        }
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def _make_engine(database_url: str):
    return create_engine(
        database_url,
        connect_args=_connect_args_for(database_url),
        pool_pre_ping=True,
    )


engine = _make_engine(settings.database_url)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def _initialize_configured_database() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_database_schema()


def initialize_database() -> None:
    """Create and patch tables, falling back to SQLite for local dev only."""
    global engine

    attempts = 1 if settings.local_database_fallback else max(
        1,
        settings.database_startup_retries,
    )
    last_error: SQLAlchemyError | None = None

    for attempt in range(1, attempts + 1):
        try:
            _initialize_configured_database()
            if attempt > 1:
                print(f"[database] connected after {attempt} attempts")
            return
        except SQLAlchemyError as exc:
            last_error = exc
            if settings.local_database_fallback:
                break
            if attempt >= attempts:
                break

            print(
                "[database] startup connection failed "
                f"(attempt {attempt}/{attempts}); retrying in "
                f"{settings.database_startup_retry_seconds}s: {exc}"
            )
            engine.dispose()
            sleep(settings.database_startup_retry_seconds)

    if not settings.local_database_fallback:
        assert last_error is not None
        raise last_error

    fallback_path = Path.cwd() / "pickbook_local.db"
    fallback_url = f"sqlite:///{fallback_path.as_posix()}"
    print(
        "[database] configured database is unavailable; "
        f"using local development SQLite at {fallback_path}"
    )

    engine.dispose()
    engine = _make_engine(fallback_url)
    SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    ensure_database_schema()


def ensure_database_schema() -> None:
    """Apply small additive schema fixes for existing deployments."""
    inspector = inspect(engine)
    dialect = engine.dialect.name
    text_type = "TEXT" if dialect != "mysql" else "LONGTEXT"

    if inspector.has_table("books"):
        existing_columns = {
            column["name"]
            for column in inspector.get_columns("books")
        }

        missing_columns = []

        if "synopsis" not in existing_columns:
            missing_columns.append(("synopsis", text_type))

        if "chapters_count" not in existing_columns:
            missing_columns.append(("chapters_count", "INTEGER"))

        if "chapter_content" not in existing_columns:
            missing_columns.append(("chapter_content", text_type))

        with engine.begin() as connection:
            for name, column_type in missing_columns:
                connection.execute(
                    text(f"ALTER TABLE books ADD COLUMN {name} {column_type}")
                )

    if inspector.has_table("profiles"):
        profile_columns = {
            column["name"]
            for column in inspector.get_columns("profiles")
        }
        if "subscription_expiry" not in profile_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE profiles ADD COLUMN subscription_expiry TIMESTAMP")
                )

        profile_missing_columns = []

        if "referral_code" not in profile_columns:
            profile_missing_columns.append(("referral_code", "VARCHAR"))

        if "referred_by" not in profile_columns:
            profile_missing_columns.append(("referred_by", "VARCHAR"))

        if "referral_bonus_claimed_at" not in profile_columns:
            profile_missing_columns.append(("referral_bonus_claimed_at", "TIMESTAMP"))

        if "signup_fingerprint" not in profile_columns:
            profile_missing_columns.append(("signup_fingerprint", "VARCHAR"))

        if profile_missing_columns:
            with engine.begin() as connection:
                for name, column_type in profile_missing_columns:
                    connection.execute(
                        text(f"ALTER TABLE profiles ADD COLUMN {name} {column_type}")
                    )

    if inspector.has_table("coupon_claims"):
        claim_columns = {
            column["name"]
            for column in inspector.get_columns("coupon_claims")
        }
        if "fingerprint" not in claim_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE coupon_claims ADD COLUMN fingerprint VARCHAR")
                )
