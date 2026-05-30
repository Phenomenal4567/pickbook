"""add profile terms acceptance fields

Revision ID: 002_profile_terms
Revises: 001_content_domains
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa


revision = "002_profile_terms"
down_revision = "001_content_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "profiles",
        sa.Column("terms_version", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("profiles", "terms_version")
    op.drop_column("profiles", "terms_accepted_at")
