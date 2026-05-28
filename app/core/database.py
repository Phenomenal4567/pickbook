from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings

connect_args = {}

if settings.database_url.startswith("postgres"):
    connect_args["sslmode"] = settings.database_sslmode

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


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
