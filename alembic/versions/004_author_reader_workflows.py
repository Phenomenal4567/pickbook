"""add author and reader workflow tables

Revision ID: 004_author_reader_workflows
Revises: 003_profile_password_hash
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa


revision = "004_author_reader_workflows"
down_revision = "003_profile_password_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("review_feedback", sa.Text(), nullable=True))
    op.add_column("stories", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("stories", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("stories", sa.Column("unpublished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("profiles", sa.Column("author_bio", sa.Text(), nullable=True))
    op.add_column("profiles", sa.Column("bank_name", sa.String(), nullable=True))
    op.add_column("profiles", sa.Column("bank_account_name", sa.String(), nullable=True))
    op.add_column("profiles", sa.Column("bank_account_number", sa.String(), nullable=True))

    op.create_table(
        "reader_engagement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("book_id", sa.Integer(), nullable=True),
        sa.Column("story_id", sa.Integer(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("liked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("comment_status", sa.String(), nullable=False, server_default="pending_review"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(book_id IS NOT NULL AND story_id IS NULL) OR (book_id IS NULL AND story_id IS NOT NULL)",
            name="ck_reader_engagement_one_content",
        ),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"]),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "book_id", name="uq_reader_engagement_user_book"),
        sa.UniqueConstraint("user_id", "story_id", name="uq_reader_engagement_user_story"),
    )
    op.create_index(op.f("ix_reader_engagement_id"), "reader_engagement", ["id"], unique=False)
    op.create_index(op.f("ix_reader_engagement_book_id"), "reader_engagement", ["book_id"], unique=False)
    op.create_index(op.f("ix_reader_engagement_story_id"), "reader_engagement", ["story_id"], unique=False)
    op.create_index(op.f("ix_reader_engagement_user_id"), "reader_engagement", ["user_id"], unique=False)
    op.create_index(op.f("ix_reader_engagement_comment_status"), "reader_engagement", ["comment_status"], unique=False)

    op.create_table(
        "author_earnings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=False),
        sa.Column("story_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("amount_kobo", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="available"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_author_earnings_id"), "author_earnings", ["id"], unique=False)
    op.create_index(op.f("ix_author_earnings_author_id"), "author_earnings", ["author_id"], unique=False)
    op.create_index(op.f("ix_author_earnings_story_id"), "author_earnings", ["story_id"], unique=False)
    op.create_index(op.f("ix_author_earnings_source"), "author_earnings", ["source"], unique=False)
    op.create_index(op.f("ix_author_earnings_status"), "author_earnings", ["status"], unique=False)

    op.create_table(
        "withdrawal_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=False),
        sa.Column("amount_kobo", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("bank_name", sa.String(), nullable=True),
        sa.Column("bank_account_name", sa.String(), nullable=True),
        sa.Column("bank_account_number", sa.String(), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_withdrawal_requests_id"), "withdrawal_requests", ["id"], unique=False)
    op.create_index(op.f("ix_withdrawal_requests_author_id"), "withdrawal_requests", ["author_id"], unique=False)
    op.create_index(op.f("ix_withdrawal_requests_status"), "withdrawal_requests", ["status"], unique=False)


def downgrade() -> None:
    op.drop_table("withdrawal_requests")
    op.drop_table("author_earnings")
    op.drop_table("reader_engagement")
    op.drop_column("profiles", "bank_account_number")
    op.drop_column("profiles", "bank_account_name")
    op.drop_column("profiles", "bank_name")
    op.drop_column("profiles", "author_bio")
    op.drop_column("stories", "unpublished_at")
    op.drop_column("stories", "published_at")
    op.drop_column("stories", "reviewed_at")
    op.drop_column("stories", "review_feedback")
