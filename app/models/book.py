from sqlalchemy import Column, Date, DateTime, Integer, String, Text, UniqueConstraint
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


class Profile(Base):

    __tablename__ = "profiles"

    id = Column(String, primary_key=True, index=True)

    email = Column(String, unique=True, index=True, nullable=False)

    current_plan = Column(String, default="free", nullable=False)

    paystack_customer_id = Column(String, nullable=True)

    subscription_expiry = Column(DateTime(timezone=True), nullable=True)

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

    claimed_at = Column(DateTime(timezone=True), server_default=func.now())


class PaystackEvent(Base):

    __tablename__ = "paystack_events"

    id = Column(Integer, primary_key=True, index=True)

    reference = Column(String, unique=True, nullable=False, index=True)

    user_id = Column(String, nullable=False, index=True)

    plan = Column(String, nullable=False)

    amount = Column(Integer, nullable=False)

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
