from datetime import datetime, timedelta, timezone
import json

import bleach
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func
from app.core.chapter_text import plain_text_to_html_paragraphs
from app.core.limiter import limiter
from app.core.database import SessionLocal
from app.core.config import settings
from app.models.book import (
    Announcement,
    AuthorApplication,
    AuthorEarning,
    AuthorNotification,
    AppSetting,
    Book,
    Chapter,
    Coupon,
    CouponClaim,
    Draft,
    PaystackEvent,
    PremiumRead,
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


class StoryOriginalStatusPayload(BaseModel):
    original_status: str


class AuthorApplicationDecisionPayload(BaseModel):
    feedback: str | None = None


class CommentModerationPayload(BaseModel):
    moderation_note: str | None = None


class PayoutDecisionPayload(BaseModel):
    status: str = "paid"
    admin_note: str | None = None


class PayoutSettingsPayload(BaseModel):
    minimum_payout_naira: int = 1000


class AnnouncementPayload(BaseModel):
    title: str
    body_html: str
    image_url: str | None = None
    priority: str = "normal"
    publish_at: datetime | None = None
    expires_at: datetime | None = None
    pinned: bool = False
    audience: str = "all"
    deep_link_url: str | None = None
    critical_repeat_session: bool = True


MINIMUM_PAYOUT_KEY = "minimum_author_payout_naira"
ANNOUNCEMENT_TAGS = [
    "p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li",
    "a", "blockquote", "code", "pre", "h3", "h4",
]
ANNOUNCEMENT_ATTRS = {
    "a": ["href", "title", "target", "rel"],
}
ANNOUNCEMENT_PRIORITIES = {"normal", "important", "critical"}
ANNOUNCEMENT_AUDIENCES = {"all", "free", "premium", "authors", "admins"}


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _clean_announcement_html(value: str) -> str:
    html = bleach.clean(
        value or "",
        tags=ANNOUNCEMENT_TAGS,
        attributes=ANNOUNCEMENT_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    ).strip()
    if not html:
        raise HTTPException(status_code=400, detail="Announcement message is required.")
    return html[:20000]


def _clean_optional_url(value: str | None, *, allow_relative: bool = False) -> str | None:
    clean = (value or "").strip()
    if not clean:
        return None
    if allow_relative and clean.startswith("/"):
        return clean[:1000]
    if clean.startswith("https://") or clean.startswith("http://"):
        return clean[:1000]
    raise HTTPException(status_code=400, detail="URL must be http(s) or a PickBook relative path.")


def _clean_original_status(value: str | None) -> str:
    status = (value or "standard").strip().lower().replace("-", "_")
    if status in {"pickbook_original", "original", "exclusive"}:
        return "pickbook_original"
    if status == "standard":
        return "standard"
    raise HTTPException(status_code=400, detail="Original status must be standard or pickbook_original.")


def _announcement_row(row: Announcement, include_body: bool = True) -> dict:
    now = _now()
    publish_at = _as_utc(row.publish_at)
    expires_at = _as_utc(row.expires_at)
    return {
        "id": row.id,
        "title": row.title,
        "body_html": row.body_html if include_body else None,
        "image_url": row.image_url,
        "priority": row.priority,
        "publish_at": row.publish_at,
        "expires_at": row.expires_at,
        "pinned": bool(row.pinned),
        "audience": row.audience,
        "deep_link_url": row.deep_link_url,
        "critical_repeat_session": bool(row.critical_repeat_session),
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "is_active": (
            publish_at <= now
            and (expires_at is None or expires_at > now)
        ) if publish_at else False,
    }


def _apply_announcement_payload(row: Announcement, payload: AnnouncementPayload, request: Request) -> Announcement:
    title = bleach.clean(payload.title or "", tags=[], strip=True).strip()
    if not title:
        raise HTTPException(status_code=400, detail="Announcement title is required.")

    priority = (payload.priority or "normal").lower().strip()
    if priority not in ANNOUNCEMENT_PRIORITIES:
        raise HTTPException(status_code=400, detail="Priority must be normal, important, or critical.")

    audience = (payload.audience or "all").lower().strip()
    if audience not in ANNOUNCEMENT_AUDIENCES:
        raise HTTPException(status_code=400, detail="Invalid announcement audience.")

    publish_at = payload.publish_at or _now()
    expires_at = payload.expires_at
    if publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=timezone.utc)
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at and expires_at <= publish_at:
        raise HTTPException(status_code=400, detail="Expiration date must be after publish date.")

    row.title = title[:255]
    row.body_html = _clean_announcement_html(payload.body_html)
    row.image_url = _clean_optional_url(payload.image_url)
    row.priority = priority
    row.publish_at = publish_at
    row.expires_at = expires_at
    row.pinned = 1 if payload.pinned else 0
    row.audience = audience
    row.deep_link_url = _clean_optional_url(payload.deep_link_url, allow_relative=True)
    row.critical_repeat_session = 1 if payload.critical_repeat_session else 0
    row.created_by = row.created_by or _admin_id(request)
    row.updated_at = _now()
    return row


def _premium_read_rows(db, start: datetime | None = None, end: datetime | None = None, author_id: str | None = None, book_id: int | None = None, genre: str | None = None):
    query = (
        db.query(PremiumRead, Book, Story, Profile)
        .outerjoin(Book, PremiumRead.book_id == Book.id)
        .outerjoin(Story, PremiumRead.story_id == Story.id)
        .outerjoin(Profile, PremiumRead.author_id == Profile.id)
    )
    if start:
        query = query.filter(PremiumRead.first_read_at >= start)
    if end:
        query = query.filter(PremiumRead.first_read_at < end)
    if author_id:
        query = query.filter(PremiumRead.author_id == author_id)
    if book_id:
        query = query.filter(PremiumRead.book_id == book_id)
    if genre:
        query = query.filter(func.lower(PremiumRead.genre) == genre.lower())
    return query.all()


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
        pending_authors = db.query(AuthorApplication).filter(
            AuthorApplication.status == "pending",
        ).count()
        active_announcements = db.query(Announcement).filter(
            Announcement.publish_at <= _now(),
            (Announcement.expires_at.is_(None)) | (Announcement.expires_at > _now()),
        ).count()
        return {
            "system_status": "healthy",
            "pending_reviews": pending_reviews,
            "rejected_stories": rejected,
            "approved_today": approved_today,
            "active_users": active_users,
            "pending_comments": pending_comments,
            "pending_withdrawals": pending_withdrawals,
            "pending_authors": pending_authors,
            "active_announcements": active_announcements,
        }


@router.get("/announcements")
@limiter.limit("60/minute")
def list_admin_announcements(
    request: Request,
    status: str = Query("all"),
):
    if status not in {"all", "active", "scheduled", "expired"}:
        raise HTTPException(status_code=400, detail="Invalid announcement status filter.")
    now = _now()
    with SessionLocal() as db:
        query = db.query(Announcement)
        if status == "active":
            query = query.filter(
                Announcement.publish_at <= now,
                (Announcement.expires_at.is_(None)) | (Announcement.expires_at > now),
            )
        elif status == "scheduled":
            query = query.filter(Announcement.publish_at > now)
        elif status == "expired":
            query = query.filter(Announcement.expires_at.isnot(None), Announcement.expires_at <= now)
        rows = (
            query.order_by(
                Announcement.pinned.desc(),
                Announcement.publish_at.desc(),
                Announcement.id.desc(),
            )
            .limit(200)
            .all()
        )
        return [_announcement_row(row) for row in rows]


@router.post("/announcements")
@limiter.limit("30/hour")
def create_announcement(payload: AnnouncementPayload, request: Request):
    with SessionLocal() as db:
        row = _apply_announcement_payload(Announcement(), payload, request)
        db.add(row)
        db.commit()
        db.refresh(row)
        return _announcement_row(row)


@router.put("/announcements/{announcement_id}")
@limiter.limit("60/hour")
def update_announcement(announcement_id: int, payload: AnnouncementPayload, request: Request):
    with SessionLocal() as db:
        row = db.query(Announcement).filter(Announcement.id == announcement_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Announcement not found.")
        row = _apply_announcement_payload(row, payload, request)
        db.add(row)
        db.commit()
        db.refresh(row)
        return _announcement_row(row)


@router.delete("/announcements")
@limiter.limit("10/hour")
def delete_all_announcements(request: Request):
    with SessionLocal() as db:
        deleted = db.query(Announcement).delete(synchronize_session=False)
        db.commit()
        return {"status": "deleted", "deleted": deleted}


@router.delete("/announcements/{announcement_id}")
@limiter.limit("60/hour")
def delete_announcement(announcement_id: int, request: Request):
    with SessionLocal() as db:
        row = db.query(Announcement).filter(Announcement.id == announcement_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Announcement not found.")
        db.delete(row)
        db.commit()
        return {"status": "deleted", "id": announcement_id}


@router.get("/authors/applications")
@limiter.limit("60/minute")
def list_author_applications(request: Request, status: str = Query("pending")):
    allowed = {"pending", "approved", "rejected", "all"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid application status filter.")
    with SessionLocal() as db:
        query = (
            db.query(AuthorApplication, Profile)
            .outerjoin(Profile, AuthorApplication.profile_id == Profile.id)
        )
        if status != "all":
            query = query.filter(AuthorApplication.status == status)
        rows = query.order_by(AuthorApplication.submitted_at.desc(), AuthorApplication.id.desc()).limit(100).all()
        return [
            {
                "id": application.id,
                "profile_id": application.profile_id,
                "email": profile.email if profile else None,
                "pen_name": application.pen_name,
                "target_genres": json.loads(application.target_genres or "[]") if application.target_genres else [],
                "short_bio": application.short_bio,
                "writing_sample_url": application.writing_sample_url,
                "status": application.status,
                "admin_feedback": application.admin_feedback,
                "submitted_at": application.submitted_at,
                "reviewed_at": application.reviewed_at,
            }
            for application, profile in rows
        ]


@router.post("/authors/applications/{application_id}/approve")
@limiter.limit("60/minute")
def approve_author_application(application_id: int, payload: AuthorApplicationDecisionPayload, request: Request):
    with SessionLocal() as db:
        application = db.query(AuthorApplication).filter(AuthorApplication.id == application_id).first()
        if not application:
            raise HTTPException(status_code=404, detail="Author application not found.")
        profile = db.query(Profile).filter(Profile.id == application.profile_id).first()
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found.")
        now = datetime.now(timezone.utc)
        application.status = "approved"
        application.admin_feedback = payload.feedback or "Approved. Your author workspace is active."
        application.reviewed_at = now
        profile.role = "author"
        profile.author_application_status = "approved"
        profile.username = profile.username or application.pen_name
        profile.author_bio = profile.author_bio or application.short_bio
        db.add(application)
        db.add(profile)
        db.commit()
        return {"status": "approved", "profile_id": profile.id, "role": profile.role}


@router.post("/authors/applications/{application_id}/reject")
@limiter.limit("60/minute")
def reject_author_application(application_id: int, payload: AuthorApplicationDecisionPayload, request: Request):
    feedback = (payload.feedback or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="Rejection feedback is required.")
    with SessionLocal() as db:
        application = db.query(AuthorApplication).filter(AuthorApplication.id == application_id).first()
        if not application:
            raise HTTPException(status_code=404, detail="Author application not found.")
        profile = db.query(Profile).filter(Profile.id == application.profile_id).first()
        now = datetime.now(timezone.utc)
        application.status = "rejected"
        application.admin_feedback = feedback[:5000]
        application.reviewed_at = now
        if profile:
            profile.author_application_status = "rejected"
            if getattr(profile, "role", "reader") != "author":
                profile.role = "reader"
            db.add(profile)
        db.add(application)
        db.commit()
        return {"status": "rejected", "application_id": application.id}


@router.get("/premium-analytics")
@limiter.limit("60/minute")
def premium_analytics(
    request: Request,
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    author_id: str | None = Query(None),
    book_id: int | None = Query(None),
    genre: str | None = Query(None),
):
    with SessionLocal() as db:
        rows = _premium_read_rows(db, date_from, date_to, author_id, book_id, genre)
        subscribers = db.query(Profile).filter(Profile.current_plan == "standard").count()
        users = {row.user_id for row, _book, _story, _profile in rows}

        by_book = {}
        by_author = {}
        by_day = {}
        by_week = {}
        by_month = {}
        for row, book, story, author in rows:
            book_item = by_book.setdefault(
                row.book_id,
                {"book_id": row.book_id, "title": (book.title if book else None) or "Untitled", "premium_reads": 0},
            )
            book_item["premium_reads"] += 1
            author_key = row.author_id or "unknown"
            author_item = by_author.setdefault(
                author_key,
                {"author_id": row.author_id, "author_name": author.username if author else "Unknown", "premium_reads": 0},
            )
            author_item["premium_reads"] += 1
            stamp = row.first_read_at
            if stamp:
                by_day[stamp.date().isoformat()] = by_day.get(stamp.date().isoformat(), 0) + 1
                iso = stamp.date().isocalendar()
                week_key = f"{iso.year}-W{iso.week:02d}"
                by_week[week_key] = by_week.get(week_key, 0) + 1
                month_key = stamp.strftime("%Y-%m")
                by_month[month_key] = by_month.get(month_key, 0) + 1

        return {
            "total_premium_subscribers": subscribers,
            "total_premium_book_reads": len(rows),
            "total_unique_premium_readers": len(users),
            "top_premium_read_books": sorted(by_book.values(), key=lambda item: -item["premium_reads"])[:10],
            "top_authors_by_premium_reads": sorted(by_author.values(), key=lambda item: -item["premium_reads"])[:10],
            "premium_reads_by_day": [{"period": key, "reads": value} for key, value in sorted(by_day.items())],
            "premium_reads_by_week": [{"period": key, "reads": value} for key, value in sorted(by_week.items())],
            "premium_reads_by_month": [{"period": key, "reads": value} for key, value in sorted(by_month.items())],
        }


def _book_chapters_payload(db, story: Story, draft: Draft | None) -> tuple[list[dict], int]:
    """Build the reader-facing chapter list for a story being published.

    Prefers the author's real, individually authored chapters (the new
    chapter upload workflow). Falls back to the legacy single-blob draft
    content only for stories that predate that workflow.
    """
    chapters = (
        db.query(Chapter)
        .filter(Chapter.story_id == story.id, Chapter.status == "published")
        .order_by(Chapter.position.asc(), Chapter.id.asc())
        .all()
    )
    if chapters:
        payload = [
            {
                "title": chapter.title or f"Chapter {index}",
                "html": plain_text_to_html_paragraphs(chapter.content or ""),
            }
            for index, chapter in enumerate(chapters, start=1)
        ]
        return payload, len(payload)

    fallback_text = draft.content if draft and draft.content else None
    payload = [
        {
            "title": "Chapter 1",
            "html": (
                plain_text_to_html_paragraphs(fallback_text)
                if fallback_text
                else "<p>This story is being prepared for readers.</p>"
            ),
        }
    ]
    return payload, 1


def _story_row(db, story: Story, draft: Draft | None, profile: Profile | None) -> dict:
    content = draft.content if draft else None
    chapter_rows = db.query(Chapter.status).filter(Chapter.story_id == story.id).all()
    return {
        "story_id": story.id,
        "title": story.title,
        "author_id": story.author_id,
        "author_name": profile.username if profile else None,
        "author_email": profile.email if profile else None,
        "status": story.status,
        "original_status": story.original_status,
        "cover": story.cover,
        "synopsis": story.synopsis or (draft.synopsis if draft else None),
        "preview": (content or "")[:4000] if content else None,
        "chapter_count": len(chapter_rows),
        "published_chapter_count": sum(1 for (chapter_status,) in chapter_rows if chapter_status == "published"),
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
        return [_story_row(db, story, draft, profile) for story, draft, profile in rows]


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
        return [_story_row(db, story, draft, profile) for story, draft, profile in rows]


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
                **_story_row(db, story, draft, profile),
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

        chapter_payload, chapter_count = _book_chapters_payload(db, story, draft)

        if not story.published_version_id:
            book = Book(
                source="pickbook-author",
                title=story.title,
                author=(profile.username if profile and profile.username else "PickBook Author"),
                genre=story.genre or "Original",
                original_status=story.original_status,
                cover=story.cover,
                synopsis=story.synopsis or (draft.synopsis if draft else None),
                chapters_count=chapter_count,
                chapter_content=json.dumps(chapter_payload),
            )
            db.add(book)
            db.flush()
            story.published_version_id = book.id
        else:
            book = db.query(Book).filter(Book.id == story.published_version_id).first()
            if book:
                book.title = story.title
                book.cover = story.cover
                book.original_status = story.original_status
                book.synopsis = story.synopsis or book.synopsis
                book.chapters_count = chapter_count
                book.chapter_content = json.dumps(chapter_payload)
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


@router.put("/stories/{story_id}/original-status")
@limiter.limit("60/hour")
def update_story_original_status(story_id: int, payload: StoryOriginalStatusPayload, request: Request):
    with SessionLocal() as db:
        story = db.query(Story).filter(Story.id == story_id).first()
        if not story:
            raise HTTPException(status_code=404, detail="Story not found.")
        story.original_status = _clean_original_status(payload.original_status)
        story.updated_at = datetime.now(timezone.utc)
        if story.published_version_id:
            book = db.query(Book).filter(Book.id == story.published_version_id).first()
            if book:
                book.original_status = story.original_status
                db.add(book)
        _audit_story(db, story, request, "original_status_update", None, story.original_status, "Admin updated original designation.")
        db.add(story)
        db.commit()
        return {"status": "updated", "story_id": story.id, "original_status": story.original_status}


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
