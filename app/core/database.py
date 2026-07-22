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
            "prepare_threshold": None,
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


engine = _make_engine(settings.sqlalchemy_database_url)
active_database_url = settings.sqlalchemy_database_url
using_local_database_fallback = False

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
    global active_database_url, engine, using_local_database_fallback

    allow_fallback = (
        settings.local_database_fallback
        and settings.app_env != "production"
    )
    attempts = 1 if allow_fallback else max(
        1,
        settings.database_startup_retries,
    )
    last_error: SQLAlchemyError | None = None

    for attempt in range(1, attempts + 1):
        try:
            _initialize_configured_database()
            active_database_url = settings.sqlalchemy_database_url
            using_local_database_fallback = False
            if attempt > 1:
                print(f"[database] connected after {attempt} attempts")
            return
        except SQLAlchemyError as exc:
            last_error = exc
            if allow_fallback:
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

    if not allow_fallback:
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
    active_database_url = fallback_url
    using_local_database_fallback = True
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

        if "original_status" not in existing_columns:
            missing_columns.append(("original_status", "VARCHAR DEFAULT 'standard' NOT NULL"))

        if "created_at" not in existing_columns:
            missing_columns.append(("created_at", "TIMESTAMP"))

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

        if "username" not in profile_columns:
            profile_missing_columns.append(("username", "VARCHAR"))

        if "email_verified" not in profile_columns:
            profile_missing_columns.append(("email_verified", "INTEGER DEFAULT 0 NOT NULL"))

        if "password_hash" not in profile_columns:
            profile_missing_columns.append(("password_hash", "VARCHAR"))

        if "email_verification_token" not in profile_columns:
            profile_missing_columns.append(("email_verification_token", "VARCHAR"))

        if "email_verification_sent_at" not in profile_columns:
            profile_missing_columns.append(("email_verification_sent_at", "TIMESTAMP"))

        if "password_reset_token" not in profile_columns:
            profile_missing_columns.append(("password_reset_token", "VARCHAR"))

        if "password_reset_sent_at" not in profile_columns:
            profile_missing_columns.append(("password_reset_sent_at", "TIMESTAMP"))

        if "referred_by" not in profile_columns:
            profile_missing_columns.append(("referred_by", "VARCHAR"))

        if "referral_bonus_claimed_at" not in profile_columns:
            profile_missing_columns.append(("referral_bonus_claimed_at", "TIMESTAMP"))

        if "signup_fingerprint" not in profile_columns:
            profile_missing_columns.append(("signup_fingerprint", "VARCHAR"))

        if "terms_accepted_at" not in profile_columns:
            profile_missing_columns.append(("terms_accepted_at", "TIMESTAMP"))

        if "terms_version" not in profile_columns:
            profile_missing_columns.append(("terms_version", "VARCHAR"))

        if "role" not in profile_columns:
            profile_missing_columns.append(("role", "VARCHAR DEFAULT 'reader' NOT NULL"))

        if "author_application_status" not in profile_columns:
            profile_missing_columns.append(("author_application_status", "VARCHAR"))

        if "author_bio" not in profile_columns:
            profile_missing_columns.append(("author_bio", "TEXT"))

        if "email_notifications_enabled" not in profile_columns:
            profile_missing_columns.append(("email_notifications_enabled", "INTEGER DEFAULT 1 NOT NULL"))

        if "author_notifications_enabled" not in profile_columns:
            profile_missing_columns.append(("author_notifications_enabled", "INTEGER DEFAULT 1 NOT NULL"))

        if "bank_name" not in profile_columns:
            profile_missing_columns.append(("bank_name", "VARCHAR"))

        if "bank_account_name" not in profile_columns:
            profile_missing_columns.append(("bank_account_name", "VARCHAR"))

        if "bank_account_number" not in profile_columns:
            profile_missing_columns.append(("bank_account_number", "VARCHAR"))

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

    if inspector.has_table("paystack_events"):
        event_columns = {
            column["name"]
            for column in inspector.get_columns("paystack_events")
        }
        event_missing_columns = []

        if "paystack_fee" not in event_columns:
            event_missing_columns.append(("paystack_fee", "INTEGER DEFAULT 0 NOT NULL"))

        if "net_amount" not in event_columns:
            event_missing_columns.append(("net_amount", "INTEGER DEFAULT 0 NOT NULL"))

        if "referral_code" not in event_columns:
            event_missing_columns.append(("referral_code", "VARCHAR"))

        if "partner_code" not in event_columns:
            event_missing_columns.append(("partner_code", "VARCHAR"))

        if event_missing_columns:
            with engine.begin() as connection:
                for name, column_type in event_missing_columns:
                    connection.execute(
                        text(f"ALTER TABLE paystack_events ADD COLUMN {name} {column_type}")
                    )

    if not inspector.has_table("pending_payments"):
        Base.metadata.tables["pending_payments"].create(bind=engine, checkfirst=True)

    if inspector.has_table("drafts"):
        draft_columns = {
            column["name"]
            for column in inspector.get_columns("drafts")
        }
        if "manuscript_metadata_json" not in draft_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE drafts ADD COLUMN manuscript_metadata_json TEXT")
                )

    if inspector.has_table("stories"):
        story_columns = {
            column["name"]
            for column in inspector.get_columns("stories")
        }
        story_missing_columns = []

        if "review_feedback" not in story_columns:
            story_missing_columns.append(("review_feedback", "TEXT"))

        if "slug" not in story_columns:
            story_missing_columns.append(("slug", "VARCHAR"))

        if "genre" not in story_columns:
            story_missing_columns.append(("genre", "VARCHAR"))

        if "original_status" not in story_columns:
            story_missing_columns.append(("original_status", "VARCHAR DEFAULT 'standard' NOT NULL"))

        if "cover" not in story_columns:
            story_missing_columns.append(("cover", "VARCHAR"))

        if "synopsis" not in story_columns:
            story_missing_columns.append(("synopsis", "TEXT"))

        if "published_version_id" not in story_columns:
            story_missing_columns.append(("published_version_id", "INTEGER"))

        if "scheduled_version_id" not in story_columns:
            story_missing_columns.append(("scheduled_version_id", "INTEGER"))

        if "updated_at" not in story_columns:
            story_missing_columns.append(("updated_at", "TIMESTAMP"))

        if "reviewed_at" not in story_columns:
            story_missing_columns.append(("reviewed_at", "TIMESTAMP"))

        if "published_at" not in story_columns:
            story_missing_columns.append(("published_at", "TIMESTAMP"))

        if "unpublished_at" not in story_columns:
            story_missing_columns.append(("unpublished_at", "TIMESTAMP"))

        if story_missing_columns:
            with engine.begin() as connection:
                for name, column_type in story_missing_columns:
                    connection.execute(
                        text(f"ALTER TABLE stories ADD COLUMN {name} {column_type}")
                    )

    for table_name in (
        "reader_engagement",
        "author_earnings",
        "withdrawal_requests",
        "author_follows",
        "reader_achievements",
        "author_applications",
        "announcements",
        "premium_reads",
        "story_review_audits",
        "author_notifications",
        "app_settings",
        "chapters",
    ):
        if not inspector.has_table(table_name):
            Base.metadata.tables[table_name].create(bind=engine, checkfirst=True)

    if inspector.has_table("chapters"):
        chapter_columns = {
            column["name"]
            for column in inspector.get_columns("chapters")
        }
        chapter_missing_columns = []

        if "upload_error" not in chapter_columns:
            chapter_missing_columns.append(("upload_error", text_type))

        if "original_filename" not in chapter_columns:
            chapter_missing_columns.append(("original_filename", "VARCHAR"))

        if "source_extension" not in chapter_columns:
            chapter_missing_columns.append(("source_extension", "VARCHAR"))

        if "published_at" not in chapter_columns:
            chapter_missing_columns.append(("published_at", "TIMESTAMP"))

        if chapter_missing_columns:
            with engine.begin() as connection:
                for name, column_type in chapter_missing_columns:
                    connection.execute(
                        text(f"ALTER TABLE chapters ADD COLUMN {name} {column_type}")
                    )

    if inspector.has_table("reader_engagement"):
        engagement_columns = {
            column["name"]
            for column in inspector.get_columns("reader_engagement")
        }
        engagement_missing_columns = []

        if "report_count" not in engagement_columns:
            engagement_missing_columns.append(("report_count", "INTEGER DEFAULT 0 NOT NULL"))

        if "report_reason" not in engagement_columns:
            engagement_missing_columns.append(("report_reason", "TEXT"))

        if "moderation_note" not in engagement_columns:
            engagement_missing_columns.append(("moderation_note", "TEXT"))

        if "edited_at" not in engagement_columns:
            engagement_missing_columns.append(("edited_at", "TIMESTAMP"))

        if engagement_missing_columns:
            with engine.begin() as connection:
                for name, column_type in engagement_missing_columns:
                    connection.execute(
                        text(f"ALTER TABLE reader_engagement ADD COLUMN {name} {column_type}")
                    )

    if inspector.has_table("announcements"):
        announcement_columns = {
            column["name"]
            for column in inspector.get_columns("announcements")
        }
        announcement_missing_columns = []

        if "image_url" not in announcement_columns:
            announcement_missing_columns.append(("image_url", "VARCHAR"))
        if "audience" not in announcement_columns:
            announcement_missing_columns.append(("audience", "VARCHAR DEFAULT 'all' NOT NULL"))
        if "deep_link_url" not in announcement_columns:
            announcement_missing_columns.append(("deep_link_url", "VARCHAR"))
        if "critical_repeat_session" not in announcement_columns:
            announcement_missing_columns.append(("critical_repeat_session", "INTEGER DEFAULT 1 NOT NULL"))
        if "updated_at" not in announcement_columns:
            announcement_missing_columns.append(("updated_at", "TIMESTAMP"))

        if announcement_missing_columns:
            with engine.begin() as connection:
                for name, column_type in announcement_missing_columns:
                    connection.execute(
                        text(f"ALTER TABLE announcements ADD COLUMN {name} {column_type}")
                    )
