from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func
from app.core.database import Base

class Book(Base):

    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)

    source = Column(String)

    title = Column(String)

    author = Column(String)

    genre = Column(String)

    cover = Column(String)

    download = Column(String)

    language = Column(String)

    synopsis = Column(Text, nullable=True)

    chapters_count = Column(Integer, nullable=True)

    chapter_content = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Story(Base):

    __tablename__ = "stories"

    id = Column(Integer, primary_key=True, index=True)

    author_id = Column(String, ForeignKey("profiles.id"), nullable=True, index=True)

    title = Column(String, nullable=False)

    slug = Column(String, unique=True, nullable=True, index=True)

    genre = Column(String, nullable=True)

    cover = Column(String, nullable=True)

    synopsis = Column(Text, nullable=True)

    status = Column(String, default="draft", nullable=False, index=True)

    review_feedback = Column(Text, nullable=True)

    published_version_id = Column(Integer, nullable=True, index=True)

    scheduled_version_id = Column(Integer, nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    updated_at = Column(DateTime(timezone=True), nullable=True)

    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    published_at = Column(DateTime(timezone=True), nullable=True)

    unpublished_at = Column(DateTime(timezone=True), nullable=True)


class AuthorApplication(Base):

    __tablename__ = "author_applications"

    id = Column(Integer, primary_key=True, index=True)

    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    pen_name = Column(String, nullable=False)

    target_genres = Column(Text, nullable=True)

    short_bio = Column(Text, nullable=True)

    writing_sample_url = Column(String, nullable=True)

    status = Column(String, default="pending", nullable=False, index=True)

    admin_feedback = Column(Text, nullable=True)

    submitted_at = Column(DateTime(timezone=True), server_default=func.now())

    reviewed_at = Column(DateTime(timezone=True), nullable=True)


class Announcement(Base):

    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, index=True)

    title = Column(String, nullable=False)

    body_html = Column(Text, nullable=False)

    image_url = Column(String, nullable=True)

    priority = Column(String, default="normal", nullable=False, index=True)

    publish_at = Column(DateTime(timezone=True), nullable=False, index=True)

    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    pinned = Column(Integer, default=0, nullable=False, index=True)

    audience = Column(String, default="all", nullable=False, index=True)

    deep_link_url = Column(String, nullable=True)

    critical_repeat_session = Column(Integer, default=1, nullable=False)

    created_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    updated_at = Column(DateTime(timezone=True), nullable=True)


class StoryVersion(Base):

    __tablename__ = "story_versions"

    __table_args__ = (
        UniqueConstraint("story_id", "version_number", name="uq_story_version_number"),
    )

    id = Column(Integer, primary_key=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=False, index=True)

    version_number = Column(Integer, nullable=False)

    title = Column(String, nullable=False)

    synopsis = Column(Text, nullable=True)

    content = Column(Text, nullable=True)

    status = Column(String, default="pending_review", nullable=False, index=True)

    submitted_at = Column(DateTime(timezone=True), nullable=True)

    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    published_at = Column(DateTime(timezone=True), nullable=True)

    scheduled_for = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Draft(Base):

    __tablename__ = "drafts"

    __table_args__ = (
        UniqueConstraint("story_id", "author_id", name="uq_draft_story_author"),
    )

    id = Column(Integer, primary_key=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=False, index=True)

    author_id = Column(String, ForeignKey("profiles.id"), nullable=True, index=True)

    title = Column(String, nullable=False)

    synopsis = Column(Text, nullable=True)

    content = Column(Text, nullable=True)

    manuscript_metadata_json = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    updated_at = Column(DateTime(timezone=True), nullable=True)


class ReadingProgress(Base):

    __tablename__ = "reading_progress"

    __table_args__ = (
        CheckConstraint(
            "(book_id IS NOT NULL AND story_id IS NULL) OR "
            "(book_id IS NULL AND story_id IS NOT NULL)",
            name="ck_reading_progress_one_content",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    book_id = Column(Integer, ForeignKey("books.id"), nullable=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True, index=True)

    chapter = Column(Integer, default=1, nullable=False)

    percent = Column(Integer, default=0, nullable=False)

    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class Bookmark(Base):

    __tablename__ = "bookmarks"

    __table_args__ = (
        CheckConstraint(
            "(book_id IS NOT NULL AND story_id IS NULL) OR "
            "(book_id IS NULL AND story_id IS NOT NULL)",
            name="ck_bookmarks_one_content",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    book_id = Column(Integer, ForeignKey("books.id"), nullable=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True, index=True)

    chapter = Column(Integer, default=1, nullable=False)

    note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class UserLibraryItem(Base):

    __tablename__ = "user_library"

    __table_args__ = (
        CheckConstraint(
            "(book_id IS NOT NULL AND story_id IS NULL) OR "
            "(book_id IS NULL AND story_id IS NOT NULL)",
            name="ck_user_library_one_content",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    book_id = Column(Integer, ForeignKey("books.id"), nullable=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True, index=True)

    status = Column(String, default="saved", nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Download(Base):

    __tablename__ = "downloads"

    __table_args__ = (
        CheckConstraint(
            "(book_id IS NOT NULL AND story_id IS NULL) OR "
            "(book_id IS NULL AND story_id IS NOT NULL)",
            name="ck_downloads_one_content",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    book_id = Column(Integer, ForeignKey("books.id"), nullable=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True, index=True)

    device_id = Column(String, nullable=True, index=True)

    billing_cycle = Column(String, nullable=True, index=True)

    quota_consumed = Column(Integer, default=1, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SearchIndex(Base):

    __tablename__ = "search_index"

    __table_args__ = (
        CheckConstraint(
            "content_type IN ('book', 'story')",
            name="ck_search_index_content_type",
        ),
        UniqueConstraint("content_type", "content_id", name="uq_search_index_content"),
    )

    id = Column(Integer, primary_key=True, index=True)

    content_type = Column(String, nullable=False, index=True)

    content_id = Column(Integer, nullable=False, index=True)

    title = Column(String, nullable=False)

    body = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    updated_at = Column(DateTime(timezone=True), nullable=True)


class ReaderEngagement(Base):

    __tablename__ = "reader_engagement"

    __table_args__ = (
        CheckConstraint(
            "(book_id IS NOT NULL AND story_id IS NULL) OR "
            "(book_id IS NULL AND story_id IS NOT NULL)",
            name="ck_reader_engagement_one_content",
        ),
        UniqueConstraint("user_id", "book_id", name="uq_reader_engagement_user_book"),
        UniqueConstraint("user_id", "story_id", name="uq_reader_engagement_user_story"),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    book_id = Column(Integer, ForeignKey("books.id"), nullable=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True, index=True)

    rating = Column(Integer, nullable=True)

    liked = Column(Integer, default=0, nullable=False)

    comment = Column(Text, nullable=True)

    comment_status = Column(String, default="pending_review", nullable=False, index=True)

    report_count = Column(Integer, default=0, nullable=False)

    report_reason = Column(Text, nullable=True)

    moderation_note = Column(Text, nullable=True)

    edited_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    updated_at = Column(DateTime(timezone=True), nullable=True)


class PremiumRead(Base):

    __tablename__ = "premium_reads"

    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_premium_read_user_book"),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    book_id = Column(Integer, ForeignKey("books.id"), nullable=False, index=True)

    author_id = Column(String, ForeignKey("profiles.id"), nullable=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True, index=True)

    genre = Column(String, nullable=True, index=True)

    first_read_at = Column(DateTime(timezone=True), server_default=func.now())


class StoryReviewAudit(Base):

    __tablename__ = "story_review_audits"

    id = Column(Integer, primary_key=True, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=False, index=True)

    admin_id = Column(String, nullable=True, index=True)

    action = Column(String, nullable=False, index=True)

    from_status = Column(String, nullable=True)

    to_status = Column(String, nullable=True)

    note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AuthorNotification(Base):

    __tablename__ = "author_notifications"

    id = Column(Integer, primary_key=True, index=True)

    author_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True, index=True)

    kind = Column(String, nullable=False, index=True)

    title = Column(String, nullable=False)

    message = Column(Text, nullable=True)

    read_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AuthorEarning(Base):

    __tablename__ = "author_earnings"

    id = Column(Integer, primary_key=True, index=True)

    author_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True, index=True)

    source = Column(String, default="manual", nullable=False, index=True)

    amount_kobo = Column(Integer, default=0, nullable=False)

    status = Column(String, default="available", nullable=False, index=True)

    note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class WithdrawalRequest(Base):

    __tablename__ = "withdrawal_requests"

    id = Column(Integer, primary_key=True, index=True)

    author_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    amount_kobo = Column(Integer, nullable=False)

    status = Column(String, default="pending", nullable=False, index=True)

    bank_name = Column(String, nullable=True)

    bank_account_name = Column(String, nullable=True)

    bank_account_number = Column(String, nullable=True)

    admin_note = Column(Text, nullable=True)

    requested_at = Column(DateTime(timezone=True), server_default=func.now())

    processed_at = Column(DateTime(timezone=True), nullable=True)


class AuthorFollow(Base):

    __tablename__ = "author_follows"

    __table_args__ = (
        UniqueConstraint("reader_id", "author_id", name="uq_author_follow_reader_author"),
    )

    id = Column(Integer, primary_key=True, index=True)

    reader_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    author_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ReaderAchievement(Base):

    __tablename__ = "reader_achievements"

    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_reader_achievement_user_code"),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

    code = Column(String, nullable=False, index=True)

    title = Column(String, nullable=False)

    description = Column(Text, nullable=True)

    progress = Column(Integer, default=0, nullable=False)

    target = Column(Integer, default=1, nullable=False)

    earned_at = Column(DateTime(timezone=True), nullable=True)

    updated_at = Column(DateTime(timezone=True), nullable=True)


class Profile(Base):

    __tablename__ = "profiles"

    id = Column(String, primary_key=True, index=True)

    email = Column(String, unique=True, index=True, nullable=False)

    password_hash = Column(String, nullable=True)

    email_verified = Column(Integer, default=0, nullable=False)

    email_verification_token = Column(String, nullable=True, index=True)

    email_verification_sent_at = Column(DateTime(timezone=True), nullable=True)

    password_reset_token = Column(String, nullable=True, index=True)

    password_reset_sent_at = Column(DateTime(timezone=True), nullable=True)

    username = Column(String, unique=True, index=True, nullable=True)

    author_bio = Column(Text, nullable=True)

    email_notifications_enabled = Column(Integer, default=1, nullable=False)

    author_notifications_enabled = Column(Integer, default=1, nullable=False)

    bank_name = Column(String, nullable=True)

    bank_account_name = Column(String, nullable=True)

    bank_account_number = Column(String, nullable=True)

    current_plan = Column(String, default="free", nullable=False)

    paystack_customer_id = Column(String, nullable=True)

    subscription_expiry = Column(DateTime(timezone=True), nullable=True)

    referral_code = Column(String, unique=True, index=True, nullable=True)

    referred_by = Column(String, nullable=True)

    referral_bonus_claimed_at = Column(DateTime(timezone=True), nullable=True)

    signup_fingerprint = Column(String, nullable=True, index=True)

    terms_accepted_at = Column(DateTime(timezone=True), nullable=True)

    terms_version = Column(String, nullable=True)

    role = Column(String, default="reader", nullable=False)

    author_application_status = Column(String, nullable=True, index=True)

    streak_count = Column(Integer, default=0, nullable=False)

    last_read_date = Column(Date, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Coupon(Base):

    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)

    code = Column(String, unique=True, index=True, nullable=False)

    discount_type = Column(String, nullable=False)

    discount_value = Column(Integer, nullable=False)

    plan_target = Column(String, default="standard", nullable=False)

    max_uses = Column(Integer, default=1, nullable=False)

    used_count = Column(Integer, default=0, nullable=False)

    expiry_date = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CouponClaim(Base):

    __tablename__ = "coupon_claims"

    __table_args__ = (
        UniqueConstraint("user_id", "coupon_id", name="uq_coupon_claim_user_coupon"),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, nullable=False, index=True)

    coupon_id = Column(Integer, nullable=False, index=True)

    fingerprint = Column(String, nullable=True, index=True)

    claimed_at = Column(DateTime(timezone=True), server_default=func.now())


class PaystackEvent(Base):

    __tablename__ = "paystack_events"

    id = Column(Integer, primary_key=True, index=True)

    reference = Column(String, unique=True, nullable=False, index=True)

    user_id = Column(String, nullable=False, index=True)

    plan = Column(String, nullable=False)

    amount = Column(Integer, nullable=False)

    paystack_fee = Column(Integer, default=0, nullable=False)

    net_amount = Column(Integer, default=0, nullable=False)

    referral_code = Column(String, nullable=True, index=True)

    partner_code = Column(String, nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PendingPayment(Base):

    __tablename__ = "pending_payments"

    reference = Column(String, primary_key=True, index=True)

    kind = Column(String, nullable=False, default="subscription")

    user_id = Column(String, nullable=True, index=True)

    plan = Column(String, nullable=True)

    amount = Column(Integer, nullable=False)

    metadata_json = Column(Text, nullable=True)

    processed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ReadingActivity(Base):

    __tablename__ = "reading_activity"

    __table_args__ = (
        UniqueConstraint("user_id", "read_date", name="uq_reading_activity_user_date"),
    )

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, nullable=False, index=True)

    read_date = Column(Date, nullable=False, index=True)

    chapters_read = Column(Integer, default=1, nullable=False)


class AppSetting(Base):

    __tablename__ = "app_settings"

    key = Column(String, primary_key=True, index=True)

    value = Column(Text, nullable=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class PartnerDeal(Base):

    __tablename__ = "partner_deals"

    id = Column(String, primary_key=True, index=True)

    name = Column(String, nullable=False)

    email = Column(String, unique=True, index=True, nullable=False)

    referral_code = Column(String, unique=True, index=True, nullable=False)

    status = Column(String, default="active", nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InvestorDeal(Base):

    __tablename__ = "investor_deals"

    id = Column(String, primary_key=True, index=True)

    name = Column(String, nullable=False)

    email = Column(String, unique=True, index=True, nullable=False)

    plan_type = Column(String, nullable=False)

    amount = Column(Integer, nullable=False)

    roi_cap_multiplier = Column(Integer, default=2, nullable=False)

    status = Column(String, default="pending_payment", nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
