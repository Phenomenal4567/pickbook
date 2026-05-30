"""add author follows and reader achievements

Revision ID: 005_author_follows_achievements
Revises: 004_author_reader_workflows
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa


revision = "005_author_follows_achievements"
down_revision = "004_author_reader_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "author_follows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reader_id", sa.String(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(["reader_id"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reader_id", "author_id", name="uq_author_follow_reader_author"),
    )
    op.create_index(op.f("ix_author_follows_id"), "author_follows", ["id"], unique=False)
    op.create_index(op.f("ix_author_follows_reader_id"), "author_follows", ["reader_id"], unique=False)
    op.create_index(op.f("ix_author_follows_author_id"), "author_follows", ["author_id"], unique=False)

    op.create_table(
        "reader_achievements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("earned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "code", name="uq_reader_achievement_user_code"),
    )
    op.create_index(op.f("ix_reader_achievements_id"), "reader_achievements", ["id"], unique=False)
    op.create_index(op.f("ix_reader_achievements_user_id"), "reader_achievements", ["user_id"], unique=False)
    op.create_index(op.f("ix_reader_achievements_code"), "reader_achievements", ["code"], unique=False)


def downgrade() -> None:
    op.drop_table("reader_achievements")
    op.drop_table("author_follows")
