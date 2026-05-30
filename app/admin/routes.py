from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, text
from app.core.limiter import limiter
from app.core.database import SessionLocal
from app.core.config import settings
from app.models.book import (
    AuthorEarning,
    AuthorNotification,
    AppSetting,
    Book,
    Coupon,
    CouponClaim,
    Draft,
    PaystackEvent,
    Profile,
    ReaderEngagement,
    Story,
    StoryReviewAudit,
    WithdrawalRequest,
)

# Auth is applied globally in main.py via Depends(verify_admin) on include_router.
# Do NOT add a second auth dependency here — it causes double-checking and
# inconsistent error codes (401 vs 403) depending on which guard fires first.
router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
)



class CouponCreate(BaseModel):
    code: str
    discount_type: str
    discount_value: int
    plan_target: str = "standard"
    max_uses: int = 1
    expiry_date: datetime | None = None


class ReviewDecisionPayload(BaseModel):
    feedback: str | None = None


class CommentModerationPayload(BaseModel):
    moderation_note: str | None = None


class PayoutDecisionPayload(BaseModel):
    status: str = "paid"
    admin_note: str | None = None


class PayoutSettingsPayload(BaseModel):
    minimum_payout_naira: int = 1000


TRIM_SOURCE_OPTIONS = (
    "anystories",
    "royalroad",
    "freewebnovel",
    "lightnovelworld",
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINIMUM_PAYOUT_KEY = "minimum_author_payout_naira"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _admin_id(request: Request) -> str | None:
    return request.headers.get("x-admin-user") or "admin"


def _audit_story(
    db,
    story: Story,
    request: Request,
    action: str,
    from_status: str | None,
    to_status: str | None,
    note: str | None = None,
) -> None:
    db.add(
        StoryReviewAudit(
            story_id=story.id,
            admin_id=_admin_id(request),
            action=action,
            from_status=from_status,
            to_status=to_status,
            note=(note or "")[:5000] or None,
        )
    )


def _notify_author(
    db,
    story: Story,
    kind: str,
    title: str,
    message: str | None,
) -> None:
    if not story.author_id:
        return
    db.add(
        AuthorNotification(
            author_id=story.author_id,
            story_id=story.id,
            kind=kind,
            title=title[:255],
            message=(message or "")[:5000] or None,
        )
    )


def _minimum_payout_naira(db) -> int:
    row = db.query(AppSetting).filter(AppSetting.key == MINIMUM_PAYOUT_KEY).first()
    try:
        return max(1, int(row.value)) if row and row.value is not None else 1000
    except (TypeError, ValueError):
        return 1000


@router.get("/dashboard")
@limiter.limit("60/minute")
def dashboard(request: Request):
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        pending_reviews = db.query(Story).filter(Story.status == "pending_review").count()
        rejected = db.query(Story).filter(Story.status == "rejected").count()
        approved_today = db.query(Story).filter(
            Story.status == "published",
            Story.published_at >= today_start,
        ).count()
        active_users = db.query(Profile).count()
        pending_comments = db.query(ReaderEngagement).filter(
            ReaderEngagement.comment_status == "pending_review",
            ReaderEngagement.comment.isnot(None),
        ).count()
        pending_withdrawals = db.query(WithdrawalRequest).filter(
            WithdrawalRequest.status == "pending",
        ).count()
        return {
            "system_status": "healthy",
            "pending_reviews": pending_reviews,
            "rejected_stories": rejected,
            "approved_today": approved_today,
            "active_users": active_users,
            "pending_comments": pending_comments,
            "pending_withdrawals": pending_withdrawals,
        }


def _story_row(story: Story, draft: Draft | None, profile: Profile | None) -> dict:
    content = draft.content if draft else None
    return {
        "story_id": story.id,
        "title": story.title,
        "author_id": story.author_id,
        "author_name": profile.username if profile else None,
        "author_email": profile.email if profile else None,
        "status": story.status,
        "cover": story.cover,
        "synopsis": story.synopsis or (draft.synopsis if draft else None),
        "preview": (content or "")[:4000] if content else None,
        "feedback": story.review_feedback,
        "submitted_at": story.created_at,
        "updated_at": story.updated_at,
    }


@router.get("/stories/pending")
@limiter.limit("60/minute")
def pending_stories(request: Request):
    with SessionLocal() as db:
        rows = (
            db.query(Story, Draft, Profile)
            .outerjoin(Draft, Draft.story_id == Story.id)
            .outerjoin(Profile, Profile.id == Story.author_id)
            .filter(Story.status == "pending_review")
            .order_by(Story.updated_at.asc(), Story.id.asc())
            .limit(100)
            .all()
        )
        return [_story_row(story, draft, profile) for story, draft, profile in rows]


@router.get("/stories")
@limiter.limit("60/minute")
def list_stories(
    request: Request,
    status: str = Query("pending_review"),
):
    allowed = {"pending_review", "rejected", "published", "unpublished", "all"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid story status filter.")
    with SessionLocal() as db:
        query = (
            db.query(Story, Draft, Profile)
            .outerjoin(Draft, Draft.story_id == Story.id)
            .outerjoin(Profile, Profile.id == Story.author_id)
        )
        if status != "all":
            query = query.filter(Story.status == status)
        rows = (
            query.order_by(Story.updated_at.desc(), Story.id.desc())
            .limit(150)
            .all()
        )
        return [_story_row(story, draft, profile) for story, draft, profile in rows]


@router.get("/stories/{story_id}/audit")
@limiter.limit("60/minute")
def story_audit(story_id: int, request: Request):
    with SessionLocal() as db:
        rows = (
            db.query(StoryReviewAudit)
            .filter(StoryReviewAudit.story_id == story_id)
            .order_by(StoryReviewAudit.created_at.desc(), StoryReviewAudit.id.desc())
            .limit(50)
            .all()
        )
        return [
            {
                "id": row.id,
                "story_id": row.story_id,
                "admin_id": row.admin_id,
                "action": row.action,
                "from_status": row.from_status,
                "to_status": row.to_status,
                "note": row.note,
                "created_at": row.created_at,
            }
            for row in rows
        ]


@router.get("/stories/recent")
@limiter.limit("60/minute")
def recent_story_reviews(request: Request):
    with SessionLocal() as db:
        rows = (
            db.query(Story, Draft, Profile)
            .outerjoin(Draft, Draft.story_id == Story.id)
            .outerjoin(Profile, Profile.id == Story.author_id)
            .filter(Story.status.in_(["published", "rejected", "unpublished"]))
            .order_by(Story.updated_at.desc(), Story.id.desc())
            .limit(50)
            .all()
        )
        return [
            {
                **_story_row(story, draft, profile),
                "reviewed_at": story.reviewed_at,
                "published_at": story.published_at,
                "unpublished_at": story.unpublished_at,
                "published_book_id": story.published_version_id,
            }
            for story, draft, profile in rows
        ]


@router.post("/stories/{story_id}/approve")
@limiter.limit("60/minute")
def approve_story(story_id: int, payload: ReviewDecisionPayload, request: Request):
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        story = db.query(Story).filter(Story.id == story_id).first()
        if not story:
            raise HTTPException(status_code=404, detail="Story not found.")
        previous_status = story.status
        draft = db.query(Draft).filter(Draft.story_id == story.id).first()
        profile = db.query(Profile).filter(Profile.id == story.author_id).first() if story.author_id else None

        story.status = "published"
        story.review_feedback = payload.feedback or "Approved for publication."
        story.reviewed_at = now
        story.published_at = now
        story.unpublished_at = None
        story.updated_at = now
        if draft:
            story.synopsis = story.synopsis or draft.synopsis

        if not story.published_version_id:
            book = Book(
                source="pickbook-author",
                title=story.title,
                author=(profile.username if profile and profile.username else "PickBook Author"),
                genre=story.genre or "Original",
                cover=story.cover,
                synopsis=story.synopsis or (draft.synopsis if draft else None),
                chapters_count=1,
                chapter_content=json.dumps(
                    [
                        {
                            "title": "Chapter 1",
                            "html": f"<p>{(draft.content if draft and draft.content else 'This story is being prepared for readers.')}</p>",
                        }
                    ]
                ),
            )
            db.add(book)
            db.flush()
            story.published_version_id = book.id
        else:
            book = db.query(Book).filter(Book.id == story.published_version_id).first()
            if book:
                book.title = story.title
                book.cover = story.cover
                book.synopsis = story.synopsis or book.synopsis
                if profile and profile.username:
                    book.author = profile.username
                db.add(book)

        if story.author_id:
            db.add(
                AuthorEarning(
                    author_id=story.author_id,
                    story_id=story.id,
                    source="publication_bonus",
                    amount_kobo=0,
                    note="Story approved. Future reader revenue can be posted to this ledger.",
                )
            )
        _audit_story(db, story, request, "approve", previous_status, "published", payload.feedback)
        _notify_author(
            db,
            story,
            "story_published",
            "Story approved",
            story.review_feedback,
        )
        db.add(story)
        db.commit()
        return {"status": "published", "story_id": story.id, "book_id": story.published_version_id}


@router.post("/stories/{story_id}/reject")
@limiter.limit("60/minute")
def reject_story(story_id: int, payload: ReviewDecisionPayload, request: Request):
    feedback = (payload.feedback or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="Rejection feedback is required.")
    with SessionLocal() as db:
        story = db.query(Story).filter(Story.id == story_id).first()
        if not story:
            raise HTTPException(status_code=404, detail="Story not found.")
        now = datetime.now(timezone.utc)
        previous_status = story.status
        story.status = "rejected"
        story.review_feedback = feedback[:5000]
        story.reviewed_at = now
        story.updated_at = now
        _audit_story(db, story, request, "reject", previous_status, "rejected", feedback)
        _notify_author(db, story, "story_rejected", "Story needs changes", feedback)
        db.add(story)
        db.commit()
        return {"status": "rejected", "story_id": story.id}


@router.get("/engagement/comments/pending")
@limiter.limit("60/minute")
def pending_comments(request: Request):
    with SessionLocal() as db:
        rows = (
            db.query(ReaderEngagement, Book, Story, Profile)
            .outerjoin(Book, ReaderEngagement.book_id == Book.id)
            .outerjoin(Story, ReaderEngagement.story_id == Story.id)
            .outerjoin(Profile, ReaderEngagement.user_id == Profile.id)
            .filter(
                ReaderEngagement.comment_status.in_(["pending_review", "reported"]),
                ReaderEngagement.comment.isnot(None),
            )
            .order_by(ReaderEngagement.created_at.asc())
            .limit(100)
            .all()
        )
        return [
            {
                "id": engagement.id,
                "content_title": book.title if book else (story.title if story else None),
                "reader": profile.username if profile else engagement.user_id,
                "rating": engagement.rating,
                "liked": bool(engagement.liked),
                "comment": engagement.comment,
                "comment_status": engagement.comment_status,
                "report_count": engagement.report_count,
                "report_reason": engagement.report_reason,
                "moderation_note": engagement.moderation_note,
                "created_at": engagement.created_at,
            }
            for engagement, book, story, profile in rows
        ]


@router.post("/engagement/comments/{engagement_id}/{decision}")
@limiter.limit("60/minute")
def moderate_comment(
    engagement_id: int,
    decision: str,
    request: Request,
    payload: CommentModerationPayload | None = None,
):
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Decision must be approve or reject.")
    with SessionLocal() as db:
        engagement = db.query(ReaderEngagement).filter(ReaderEngagement.id == engagement_id).first()
        if not engagement:
            raise HTTPException(status_code=404, detail="Comment not found.")
        engagement.comment_status = "approved" if decision == "approve" else "rejected"
        if payload and payload.moderation_note is not None:
            engagement.moderation_note = payload.moderation_note[:2000]
        engagement.updated_at = datetime.now(timezone.utc)
        db.add(engagement)
        db.commit()
        return {"status": engagement.comment_status}


@router.get("/withdrawals")
@limiter.limit("60/minute")
def list_withdrawals(request: Request):
    with SessionLocal() as db:
        minimum_payout = _minimum_payout_naira(db)
        rows = (
            db.query(WithdrawalRequest, Profile)
            .outerjoin(Profile, WithdrawalRequest.author_id == Profile.id)
            .order_by(WithdrawalRequest.requested_at.desc(), WithdrawalRequest.id.desc())
            .limit(100)
            .all()
        )
        return [
            {
                "id": row.id,
                "author_id": row.author_id,
                "author_name": profile.username if profile else None,
                "author_email": profile.email if profile else None,
                "amount_naira": row.amount_kobo / 100,
                "status": row.status,
                "bank_name": row.bank_name,
                "bank_account_name": row.bank_account_name,
                "bank_account_number": row.bank_account_number,
                "admin_note": row.admin_note,
                "requested_at": row.requested_at,
                "processed_at": row.processed_at,
                "receipt_id": (
                    f"PB-PAYOUT-{row.id}-{row.processed_at.strftime('%Y%m%d')}"
                    if row.processed_at and row.status == "paid"
                    else None
                ),
                "minimum_payout_naira": minimum_payout,
            }
            for row, profile in rows
        ]


@router.post("/withdrawals/{withdrawal_id}")
@limiter.limit("60/minute")
def process_withdrawal(withdrawal_id: int, payload: PayoutDecisionPayload, request: Request):
    if payload.status not in {"paid", "rejected"}:
        raise HTTPException(status_code=400, detail="Status must be paid or rejected.")
    with SessionLocal() as db:
        row = db.query(WithdrawalRequest).filter(WithdrawalRequest.id == withdrawal_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Withdrawal not found.")
        row.status = payload.status
        row.admin_note = payload.admin_note
        row.processed_at = datetime.now(timezone.utc)
        db.add(row)
        db.commit()
        return {"status": row.status, "withdrawal_id": row.id}


@router.get("/payout-settings")
@limiter.limit("60/minute")
def payout_settings(request: Request):
    with SessionLocal() as db:
        return {"minimum_payout_naira": _minimum_payout_naira(db)}


@router.put("/payout-settings")
@limiter.limit("20/hour")
def update_payout_settings(payload: PayoutSettingsPayload, request: Request):
    minimum = max(1, int(payload.minimum_payout_naira))
    with SessionLocal() as db:
        row = db.query(AppSetting).filter(AppSetting.key == MINIMUM_PAYOUT_KEY).first()
        if not row:
            row = AppSetting(key=MINIMUM_PAYOUT_KEY)
        row.value = str(minimum)
        row.updated_at = datetime.now(timezone.utc)
        db.add(row)
        db.commit()
        return {"minimum_payout_naira": minimum}


@router.get("/withdrawals/export")
@limiter.limit("20/hour")
def export_withdrawals(request: Request):
    with SessionLocal() as db:
        rows = (
            db.query(WithdrawalRequest, Profile)
            .outerjoin(Profile, WithdrawalRequest.author_id == Profile.id)
            .order_by(WithdrawalRequest.requested_at.desc(), WithdrawalRequest.id.desc())
            .limit(1000)
            .all()
        )
        return [
            {
                "id": row.id,
                "author_id": row.author_id,
                "author_name": profile.username if profile else None,
                "author_email": profile.email if profile else None,
                "amount_naira": row.amount_kobo / 100,
                "status": row.status,
                "requested_at": row.requested_at,
                "processed_at": row.processed_at,
                "admin_note": row.admin_note,
                "receipt_id": (
                    f"PB-PAYOUT-{row.id}-{row.processed_at.strftime('%Y%m%d')}"
                    if row.processed_at and row.status == "paid"
                    else None
                ),
            }
            for row, profile in rows
        ]


@router.post("/coupons")
@limiter.limit("20/minute")
def create_coupon(payload: CouponCreate, request: Request):
    if payload.discount_type not in {"percent", "fixed_amount", "free_days"}:
        raise HTTPException(status_code=400, detail="Invalid coupon type.")

    if payload.plan_target != "standard":
        raise HTTPException(status_code=400, detail="Invalid plan target.")

    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Coupon code is required.")

    with SessionLocal() as db:
        if db.query(Coupon).filter(Coupon.code == code).first():
            raise HTTPException(status_code=400, detail="Coupon already exists.")

        coupon = Coupon(
            code=code,
            discount_type=payload.discount_type,
            discount_value=payload.discount_value,
            plan_target=payload.plan_target,
            max_uses=payload.max_uses,
            expiry_date=payload.expiry_date,
        )
        db.add(coupon)
        db.commit()
        db.refresh(coupon)

        return {
            "id": coupon.id,
            "code": coupon.code,
            "discount_type": coupon.discount_type,
            "discount_value": coupon.discount_value,
            "max_uses": coupon.max_uses,
            "used_count": coupon.used_count,
            "expiry_date": coupon.expiry_date,
        }


@router.get("/coupons")
@limiter.limit("60/minute")
def list_coupons(request: Request):
    with SessionLocal() as db:
        coupons = db.query(Coupon).order_by(Coupon.id.desc()).limit(100).all()
        return [
            {
                "id": coupon.id,
                "code": coupon.code,
                "discount_type": coupon.discount_type,
                "discount_value": coupon.discount_value,
                "plan_target": coupon.plan_target,
                "max_uses": coupon.max_uses,
                "used_count": coupon.used_count,
                "expiry_date": coupon.expiry_date,
            }
            for coupon in coupons
        ]


@router.get("/accounting/summary")
@limiter.limit("60/minute")
def accounting_summary(
    request: Request,
    active_partners: int = Query(1, ge=1, le=500),
    investor_plan: str = Query("revenue_share"),
    investment_amount_naira: int = Query(15000, ge=0),
    investor_cap_multiplier: float = Query(2.0, ge=1.0, le=10.0),
    investor_revenue_share_percent: float = Query(15.0, ge=0.0, le=100.0),
    investor_paid_to_date_naira: int = Query(0, ge=0),
    investor_slots: int = Query(2, ge=0, le=100),
):
    now = datetime.now(timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)

    with SessionLocal() as db:
        events = (
            db.query(PaystackEvent, Profile)
            .outerjoin(Profile, PaystackEvent.user_id == Profile.id)
            .filter(
                PaystackEvent.created_at >= period_start,
                PaystackEvent.created_at < next_month,
            )
            .all()
        )

        subscription_events = [
            (event, profile)
            for event, profile in events
            if event.plan == "standard"
        ]
        donation_events = [
            event
            for event, _profile in events
            if event.plan == "donation"
        ]

        gross_subscription_kobo = sum(event.amount for event, _profile in subscription_events)
        donation_kobo = sum(event.amount for event in donation_events)
        direct_referral_kobo = sum(
            event.amount
            for event, profile in subscription_events
            if profile and profile.referred_by
        )
        organic_kobo = max(0, gross_subscription_kobo - direct_referral_kobo)

        direct_commission_kobo = int(direct_referral_kobo * 0.20)
        marketing_pool_kobo = int(organic_kobo * 0.15)
        partner_pool_share_kobo = int(marketing_pool_kobo / active_partners)

        gross_subscription_naira = gross_subscription_kobo / 100
        investor_cap_naira = int(investment_amount_naira * investor_cap_multiplier)
        investor_cap_remaining_naira = max(
            0,
            investor_cap_naira - investor_paid_to_date_naira,
        )

        if investor_plan == "subscriber_slots":
            covered_slots = min(investor_slots, len(subscription_events))
            investor_due_naira = covered_slots * (settings.standard_plan_price_kobo / 100)
            investor_note = (
                "Subscriber-slot payout is limited by active paying subscribers "
                "recorded this month."
            )
        elif investor_plan == "revenue_share":
            investor_due_naira = gross_subscription_naira * (
                investor_revenue_share_percent / 100
            )
            investor_note = "Revenue-share payout stops once the ROI cap is reached."
        else:
            raise HTTPException(status_code=400, detail="Invalid investor plan.")

        investor_due_naira = int(min(investor_due_naira, investor_cap_remaining_naira))

        return {
            "period": {
                "start": period_start.isoformat(),
                "end": next_month.isoformat(),
            },
            "pricing": {
                "standard_plan_naira": settings.standard_plan_price_kobo / 100,
            },
            "revenue": {
                "gross_subscription_naira": gross_subscription_naira,
                "donations_naira": donation_kobo / 100,
                "direct_referral_revenue_naira": direct_referral_kobo / 100,
                "organic_revenue_naira": organic_kobo / 100,
                "subscription_count": len(subscription_events),
                "donation_count": len(donation_events),
            },
            "payouts": {
                "marketing_pool_naira": marketing_pool_kobo / 100,
                "marketing_pool_share_per_partner_naira": partner_pool_share_kobo / 100,
                "direct_referral_commission_naira": direct_commission_kobo / 100,
                "investor_due_naira": investor_due_naira,
                "total_due_naira": (
                    marketing_pool_kobo
                    + direct_commission_kobo
                    + (investor_due_naira * 100)
                ) / 100,
            },
            "terms": {
                "active_partners": active_partners,
                "marketing_pool_percent": 15,
                "direct_referral_percent": 20,
                "investor_plan": investor_plan,
                "investment_amount_naira": investment_amount_naira,
                "investor_cap_naira": investor_cap_naira,
                "investor_cap_remaining_naira": investor_cap_remaining_naira,
                "investor_revenue_share_percent": investor_revenue_share_percent,
                "investor_slots": investor_slots,
                "investor_note": investor_note,
            },
        }


@router.delete("/coupons/{coupon_id}")
@limiter.limit("30/minute")
def delete_coupon(coupon_id: int, request: Request):
    with SessionLocal() as db:
        coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
        if not coupon:
            raise HTTPException(status_code=404, detail="Coupon not found.")

        code = coupon.code
        db.query(CouponClaim).filter(CouponClaim.coupon_id == coupon.id).delete()
        db.delete(coupon)
        db.commit()

        return {
            "status": "deleted",
            "id": coupon_id,
            "code": code,
        }


@router.post("/seed/compact-books")
@limiter.limit("3/hour")
def seed_compact_books(request: Request):
    seed_path = PROJECT_ROOT / "books.compact.sql"
    if not seed_path.exists():
        raise HTTPException(status_code=404, detail="books.compact.sql was not found.")

    rows = []
    in_books_copy = False

    with seed_path.open("r", encoding="utf-8", errors="replace") as seed_file:
        for line in seed_file:
            if line.startswith("COPY public.books "):
                in_books_copy = True
                continue

            if in_books_copy and line == "\\.\n":
                break

            if not in_books_copy:
                continue

            columns = line.rstrip("\n").split("\t")
            if len(columns) != 11:
                continue

            rows.append(columns)

    inserted = 0
    skipped = 0
    max_id = 0

    with SessionLocal() as db:
        existing_downloads = {
            value
            for (value,) in db.query(Book.download).filter(Book.download.isnot(None)).all()
        }

        for columns in rows:
            book_id = int(columns[0])
            download = None if columns[6] == r"\N" else columns[6]

            if download and download in existing_downloads:
                skipped += 1
                continue

            db.add(
                Book(
                    id=book_id,
                    source=None if columns[1] == r"\N" else columns[1],
                    title=None if columns[2] == r"\N" else columns[2],
                    author=None if columns[3] == r"\N" else columns[3],
                    genre=None if columns[4] == r"\N" else columns[4],
                    cover=None if columns[5] == r"\N" else columns[5],
                    download=download,
                    language=None if columns[7] == r"\N" else columns[7],
                    synopsis=None if columns[8] == r"\N" else columns[8],
                    chapters_count=None if columns[9] == r"\N" else int(columns[9]),
                    chapter_content=None if columns[10] == r"\N" else columns[10],
                )
            )
            inserted += 1
            max_id = max(max_id, book_id)
            if download:
                existing_downloads.add(download)

        db.commit()

        if inserted and db.bind and db.bind.dialect.name == "postgresql":
            db.execute(
                text(
                    "SELECT setval("
                    "'books_id_seq'::regclass, "
                    "GREATEST((SELECT COALESCE(MAX(id), 1) FROM books), 1), "
                    "true)"
                )
            )
            db.commit()

    return {
        "status": "seeded",
        "source": str(seed_path.name),
        "inserted": inserted,
        "skipped": skipped,
        "max_id": max_id,
    }


@router.post("/storage/purge-chapter-cache")
@limiter.limit("6/hour")
def purge_chapter_cache(
    request: Request,
    source: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    dry_run: bool = Query(False),
):
    with SessionLocal() as db:
        query = db.query(Book).filter(Book.chapter_content.isnot(None))
        if source:
            query = query.filter(Book.source == source.strip().lower())

        total_matching = query.count()
        books = query.order_by(Book.id.asc()).limit(limit).all()
        approx_bytes = sum(
            len((book.chapter_content or "").encode("utf-8"))
            for book in books
        )

        if not dry_run:
            for book in books:
                book.chapter_content = None
            db.commit()

        return {
            "status": "dry_run" if dry_run else "purged",
            "source": source or "all",
            "matched": total_matching,
            "processed": len(books),
            "approx_bytes_removed": 0 if dry_run else approx_bytes,
            "approx_bytes_that_would_be_removed": approx_bytes if dry_run else 0,
            "note": (
                "Postgres may need VACUUM or a Railway restart/recovery cycle "
                "before volume usage visibly drops."
            ),
        }


def _book_storage_weight(book: Book) -> int:
    return sum(
        len((value or "").encode("utf-8"))
        for value in (
            book.title,
            book.author,
            book.genre,
            book.cover,
            book.download,
            book.language,
            book.synopsis,
            book.chapter_content,
        )
    )


def _cached_chapter_count(chapter_content: str | None) -> int:
    if not chapter_content:
        return 0

    try:
        chapters = json.loads(chapter_content)
    except json.JSONDecodeError:
        return 0

    if not isinstance(chapters, list):
        return 0

    return sum(
        1
        for chapter in chapters
        if isinstance(chapter, dict) and chapter.get("html")
    )


def _ingest_status(book: Book) -> str:
    cached = _cached_chapter_count(book.chapter_content)
    total = book.chapters_count or 0

    if total > 0 and cached >= total:
        return "full"

    if cached > 0:
        return "partial"

    return "incomplete"


@router.get("/books/source-summary")
@limiter.limit("60/minute")
def source_summary(request: Request):
    with SessionLocal() as db:
        books = db.query(Book).all()

    summary = {}
    for book in books:
        source = (book.source or "unknown").lower()
        row = summary.setdefault(
            source,
            {
                "source": source,
                "total": 0,
                "full": 0,
                "partial": 0,
                "incomplete": 0,
            },
        )
        row["total"] += 1
        row[_ingest_status(book)] += 1

    return sorted(
        summary.values(),
        key=lambda item: (-item["total"], item["source"]),
    )


def _trim_source_rows(
    request: Request,
    target_remaining_percent: int = Query(65, ge=1, le=99),
    dry_run: bool = Query(True),
    source: str = Query("all"),
):
    normalized_source = source.strip().lower()
    if normalized_source in {"", "all", "*"}:
        normalized_source = "all"
    elif normalized_source not in TRIM_SOURCE_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Source must be all, anystories, royalroad, freewebnovel, "
                "or lightnovelworld."
            ),
        )

    with SessionLocal() as db:
        query = db.query(Book)
        if normalized_source != "all":
            query = query.filter(func.lower(Book.source) == normalized_source)

        books = query.all()
        weighted_books = [(book, _book_storage_weight(book)) for book in books]

        total_bytes = sum(size for _book, size in weighted_books)
        bytes_to_remove = int(total_bytes * ((100 - target_remaining_percent) / 100))
        weighted_books.sort(key=lambda item: item[1], reverse=True)

        selected = []
        selected_bytes = 0
        for book, approx_bytes in weighted_books:
            if selected_bytes >= bytes_to_remove:
                break
            selected.append(book)
            selected_bytes += approx_bytes

        by_source = {}
        for book, _approx_bytes in weighted_books:
            source_name = (book.source or "unknown").lower()
            by_source.setdefault(source_name, {"matched": 0, "selected": 0})
            by_source[source_name]["matched"] += 1
        for book in selected:
            source_name = (book.source or "unknown").lower()
            by_source.setdefault(source_name, {"matched": 0, "selected": 0})
            by_source[source_name]["selected"] += 1

        if not dry_run and selected:
            for book in selected:
                db.delete(book)
            db.commit()

        return {
            "status": "dry_run" if dry_run else "trimmed",
            "source": normalized_source,
            "sources": sorted(by_source),
            "available_sources": list(TRIM_SOURCE_OPTIONS),
            "target_remaining_percent": target_remaining_percent,
            "matched": len(books),
            "selected": len(selected),
            "approx_total_bytes": total_bytes,
            "approx_bytes_removed": 0 if dry_run else selected_bytes,
            "approx_bytes_that_would_be_removed": selected_bytes if dry_run else 0,
            "by_source": by_source,
            "note": (
                "This removes the largest rows first for the selected source scope. "
                "Railway/Postgres may still need VACUUM FULL or a database restart "
                "cycle before volume usage visibly falls."
            ),
        }


@router.post("/storage/trim-sources")
@limiter.limit("30/hour")
def trim_sources(
    request: Request,
    target_remaining_percent: int = Query(65, ge=1, le=99),
    dry_run: bool = Query(True),
    source: str = Query("all"),
):
    return _trim_source_rows(
        request=request,
        target_remaining_percent=target_remaining_percent,
        dry_run=dry_run,
        source=source,
    )


@router.post("/storage/trim-webnovel-sources")
@limiter.limit("30/hour")
def trim_webnovel_sources(
    request: Request,
    target_remaining_percent: int = Query(65, ge=1, le=99),
    dry_run: bool = Query(True),
):
    return _trim_source_rows(
        request=request,
        target_remaining_percent=target_remaining_percent,
        dry_run=dry_run,
        source="all",
    )
