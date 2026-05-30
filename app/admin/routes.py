from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, text
from app.core.limiter import limiter
from app.core.database import SessionLocal
from app.core.config import settings
from app.models.book import Book, Coupon, CouponClaim, PaystackEvent, Profile

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


WEB_NOVEL_TRIM_SOURCES = ("royalroad", "freewebnovel", "lightnovelworld")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/dashboard")
@limiter.limit("60/minute")
def dashboard(request: Request):
    return {
        "system_status": "healthy",
        "pending_reviews": 12,
        "quarantined_books": 3,
        "approved_today": 42,
        "active_users": 1280,
    }


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


@router.post("/storage/trim-webnovel-sources")
@limiter.limit("3/hour")
def trim_webnovel_sources(
    request: Request,
    target_remaining_percent: int = Query(65, ge=1, le=99),
    dry_run: bool = Query(True),
):
    with SessionLocal() as db:
        books = (
            db.query(Book)
            .filter(func.lower(Book.source).in_(WEB_NOVEL_TRIM_SOURCES))
            .all()
        )

        weighted_books = []
        for book in books:
            approx_bytes = sum(
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
            weighted_books.append((book, approx_bytes))

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

        by_source = {source: {"matched": 0, "selected": 0} for source in WEB_NOVEL_TRIM_SOURCES}
        for book, _approx_bytes in weighted_books:
            source = (book.source or "").lower()
            if source in by_source:
                by_source[source]["matched"] += 1
        for book in selected:
            source = (book.source or "").lower()
            if source in by_source:
                by_source[source]["selected"] += 1

        if not dry_run and selected:
            for book in selected:
                db.delete(book)
            db.commit()

        return {
            "status": "dry_run" if dry_run else "trimmed",
            "sources": list(WEB_NOVEL_TRIM_SOURCES),
            "target_remaining_percent": target_remaining_percent,
            "matched": len(books),
            "selected": len(selected),
            "approx_total_bytes": total_bytes,
            "approx_bytes_removed": 0 if dry_run else selected_bytes,
            "approx_bytes_that_would_be_removed": selected_bytes if dry_run else 0,
            "by_source": by_source,
            "note": (
                "This removes the largest rows from Royal Road, FreeWebNovel, "
                "and LightNovelWorld first. Railway/Postgres may still need "
                "VACUUM FULL or a database restart cycle before volume usage "
                "visibly falls."
            ),
        }
