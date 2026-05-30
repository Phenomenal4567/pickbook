"""add profile password hash

Revision ID: 003_profile_password_hash
Revises: 002_profile_terms
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa


revision = "003_profile_password_hash"
down_revision = "002_profile_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column("password_hash", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("profiles", "password_hash")
