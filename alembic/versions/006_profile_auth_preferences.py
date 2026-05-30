"""add profile auth and notification preferences

Revision ID: 006_profile_auth_preferences
Revises: 005_author_follows_achievements
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa


revision = "006_profile_auth_preferences"
down_revision = "005_author_follows_achievements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("email_verification_token", sa.String(), nullable=True))
    op.add_column("profiles", sa.Column("email_verification_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("profiles", sa.Column("password_reset_token", sa.String(), nullable=True))
    op.add_column("profiles", sa.Column("password_reset_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "profiles",
        sa.Column("email_notifications_enabled", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "profiles",
        sa.Column("author_notifications_enabled", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        op.f("ix_profiles_email_verification_token"),
        "profiles",
        ["email_verification_token"],
        unique=False,
    )
    op.create_index(
        op.f("ix_profiles_password_reset_token"),
        "profiles",
        ["password_reset_token"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_profiles_password_reset_token"), table_name="profiles")
    op.drop_index(op.f("ix_profiles_email_verification_token"), table_name="profiles")
    op.drop_column("profiles", "author_notifications_enabled")
    op.drop_column("profiles", "email_notifications_enabled")
    op.drop_column("profiles", "password_reset_sent_at")
    op.drop_column("profiles", "password_reset_token")
    op.drop_column("profiles", "email_verification_sent_at")
    op.drop_column("profiles", "email_verification_token")
