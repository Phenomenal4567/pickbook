from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings

engine = create_engine(settings.database_url)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def ensure_database_schema() -> None:
    """Apply small additive schema fixes for existing deployments."""
    inspector = inspect(engine)
    if not inspector.has_table("books"):
        return

    existing_columns = {
        column["name"]
        for column in inspector.get_columns("books")
    }

    dialect = engine.dialect.name
    text_type = "TEXT" if dialect != "mysql" else "LONGTEXT"

    missing_columns = []

    if "synopsis" not in existing_columns:
        missing_columns.append(("synopsis", text_type))

    if "chapters_count" not in existing_columns:
        missing_columns.append(("chapters_count", "INTEGER"))

    if "chapter_content" not in existing_columns:
        missing_columns.append(("chapter_content", text_type))

    if not missing_columns:
        return

    with engine.begin() as connection:
        for name, column_type in missing_columns:
            connection.execute(
                text(f"ALTER TABLE books ADD COLUMN {name} {column_type}")
            )
