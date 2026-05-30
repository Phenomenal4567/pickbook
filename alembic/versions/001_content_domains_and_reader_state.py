"""add content domains and reader state tables

Revision ID: 001_content_domains
Revises:
Create Date: 2026-07-09
"""

from alembic import op
import sqlalchemy as sa


revision = "001_content_domains"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("author_id", sa.String(), sa.ForeignKey("profiles.id"), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=True),
        sa.Column("genre", sa.String(), nullable=True),
        sa.Column("cover", sa.String(), nullable=True),
        sa.Column("synopsis", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("published_version_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_version_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_stories_author_id", "stories", ["author_id"])
    op.create_index("ix_stories_slug", "stories", ["slug"], unique=True)
    op.create_index("ix_stories_status", "stories", ["status"])

    op.create_table(
        "story_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("synopsis", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_review"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("story_id", "version_number", name="uq_story_version_number"),
    )
    op.create_index("ix_story_versions_story_id", "story_versions", ["story_id"])
    op.create_index("ix_story_versions_status", "story_versions", ["status"])

    op.create_table(
        "drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("author_id", sa.String(), sa.ForeignKey("profiles.id"), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("synopsis", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("manuscript_metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("story_id", "author_id", name="uq_draft_story_author"),
    )
    op.create_index("ix_drafts_story_id", "drafts", ["story_id"])
    op.create_index("ix_drafts_author_id", "drafts", ["author_id"])

    one_content_check = (
        "(book_id IS NOT NULL AND story_id IS NULL) OR "
        "(book_id IS NULL AND story_id IS NOT NULL)"
    )

    op.create_table(
        "reading_progress",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id"), nullable=True),
        sa.Column("chapter", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(one_content_check, name="ck_reading_progress_one_content"),
    )
    op.create_index("ix_reading_progress_user_id", "reading_progress", ["user_id"])
    op.create_index("ix_reading_progress_book_id", "reading_progress", ["book_id"])
    op.create_index("ix_reading_progress_story_id", "reading_progress", ["story_id"])

    op.create_table(
        "bookmarks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id"), nullable=True),
        sa.Column("chapter", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(one_content_check, name="ck_bookmarks_one_content"),
    )
    op.create_index("ix_bookmarks_user_id", "bookmarks", ["user_id"])
    op.create_index("ix_bookmarks_book_id", "bookmarks", ["book_id"])
    op.create_index("ix_bookmarks_story_id", "bookmarks", ["story_id"])

    op.create_table(
        "user_library",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="saved"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(one_content_check, name="ck_user_library_one_content"),
    )
    op.create_index("ix_user_library_user_id", "user_library", ["user_id"])
    op.create_index("ix_user_library_book_id", "user_library", ["book_id"])
    op.create_index("ix_user_library_story_id", "user_library", ["story_id"])

    op.create_table(
        "downloads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id"), nullable=True),
        sa.Column("device_id", sa.String(), nullable=True),
        sa.Column("billing_cycle", sa.String(), nullable=True),
        sa.Column("quota_consumed", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(one_content_check, name="ck_downloads_one_content"),
    )
    op.create_index("ix_downloads_user_id", "downloads", ["user_id"])
    op.create_index("ix_downloads_book_id", "downloads", ["book_id"])
    op.create_index("ix_downloads_story_id", "downloads", ["story_id"])

    op.create_table(
        "search_index",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("content_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("content_type IN ('book', 'story')", name="ck_search_index_content_type"),
        sa.UniqueConstraint("content_type", "content_id", name="uq_search_index_content"),
    )
    op.create_index("ix_search_index_content_type", "search_index", ["content_type"])
    op.create_index("ix_search_index_content_id", "search_index", ["content_id"])


def downgrade() -> None:
    op.drop_table("search_index")
    op.drop_table("downloads")
    op.drop_table("user_library")
    op.drop_table("bookmarks")
    op.drop_table("reading_progress")
    op.drop_table("drafts")
    op.drop_table("story_versions")
    op.drop_table("stories")
