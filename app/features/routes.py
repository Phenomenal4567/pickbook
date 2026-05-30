import json
from datetime import date, datetime, timedelta, timezone

import bleach
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.core.auth import get_current_profile, profile_id_from_token, require_standard_profile
from app.core.database import SessionLocal
from app.core.limiter import limiter
from app.models.book import (
    Book,
    AuthorEarning,
    AuthorFollow,
    Bookmark,
    Download,
    Profile,
    ReadingActivity,
    ReadingProgress,
    ReaderEngagement,
    ReaderAchievement,
    Story,
    UserLibraryItem,
)

router = APIRouter(prefix="/api", tags=["Features"])


class StreakPayload(BaseModel):
    chapter: int | None = None
    book_id: int | None = None


class ReaderProgressPayload(BaseModel):
    chapter: int = 1
    pct: int | None = None
    percent: int | None = None


class ReaderStatePayload(BaseModel):
    shelf: list[int] = []
    progress: dict[str, ReaderProgressPayload] = {}
    bookmarks: dict[str, bool] = {}
    downloads: dict[str, dict] = {}


class EngagementPayload(BaseModel):
    rating: int | None = None
    liked: bool | None = None
    comment: str | None = None


class CommentReportPayload(BaseModel):
    reason: str | None = None


ACHIEVEMENT_DEFS = {
    "read_10_books": ("Read 10 Books", "Read chapters across 10 books.", 10),
    "first_premium_read": ("First Premium Read", "Open your first locked/premium chapter.", 1),
    "streak_30": ("30-Day Reading Streak", "Build a 30-day reading streak.", 30),
    "top_supporter": ("Top Supporter", "Like or review 10 stories.", 10),
    "premium_explorer": ("Premium Explorer", "Save 3 books for offline reading.", 3),
}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _chapter_list(book: Book) -> list[dict]:
    if not book.chapter_content:
        return []

    try:
        chapters = json.loads(book.chapter_content)
    except json.JSONDecodeError:
        return []

    return chapters if isinstance(chapters, list) else []


def _bounded_percent(value: int | None) -> int:
    return max(0, min(100, int(value or 0)))


def _book_ids(db, ids: set[int]) -> set[int]:
    if not ids:
        return set()
    rows = db.query(Book.id).filter(Book.id.in_(ids)).all()
    return {int(row[0]) for row in rows}


def _bookmark_parts(key: str) -> tuple[int, int] | None:
    try:
        book_id, chapter = key.split("_", 1)
        return int(book_id), int(chapter)
    except (TypeError, ValueError):
        return None


def _replace_user_book_rows(db, model, user_id: str) -> None:
    db.query(model).filter(
        model.user_id == user_id,
        model.book_id.isnot(None),
        model.story_id.is_(None),
    ).delete(synchronize_session=False)


def _reader_state_response(profile: Profile, db) -> dict:
    shelf_rows = db.query(UserLibraryItem).filter(
        UserLibraryItem.user_id == profile.id,
        UserLibraryItem.book_id.isnot(None),
        UserLibraryItem.story_id.is_(None),
    ).all()
    progress_rows = db.query(ReadingProgress).filter(
        ReadingProgress.user_id == profile.id,
        ReadingProgress.book_id.isnot(None),
        ReadingProgress.story_id.is_(None),
    ).all()
    bookmark_rows = db.query(Bookmark).filter(
        Bookmark.user_id == profile.id,
        Bookmark.book_id.isnot(None),
        Bookmark.story_id.is_(None),
    ).all()
    download_rows = db.query(Download).filter(
        Download.user_id == profile.id,
        Download.book_id.isnot(None),
        Download.story_id.is_(None),
    ).all()

    return {
        "profile": {
            "id": profile.id,
            "email": profile.email,
            "email_verified": bool(profile.email_verified),
            "username": profile.username,
            "current_plan": profile.current_plan,
            "subscription_expiry": profile.subscription_expiry,
            "referral_code": profile.referral_code,
            "streak_count": profile.streak_count,
            "last_read_date": profile.last_read_date,
        },
        "shelf": [row.book_id for row in shelf_rows if row.book_id],
        "progress": {
            str(row.book_id): {
                "chapter": row.chapter,
                "pct": row.percent,
            }
            for row in progress_rows
            if row.book_id
        },
        "bookmarks": {
            f"{row.book_id}_{row.chapter}": True
            for row in bookmark_rows
            if row.book_id
        },
        "downloads": {
            str(row.book_id): {
                "savedAt": row.created_at.isoformat() if row.created_at else None,
                "deviceId": row.device_id,
            }
            for row in download_rows
            if row.book_id
        },
    }


def _clean_comment(value: str | None) -> str | None:
    comment = bleach.clean(value or "", tags=[], strip=True).strip()
    return comment[:2000] or None


def _engagement_summary(db, book_id: int, profile: Profile | None = None) -> dict:
    rows = db.query(ReaderEngagement, Profile).outerjoin(
        Profile,
        ReaderEngagement.user_id == Profile.id,
    ).filter(ReaderEngagement.book_id == book_id).all()
    engagement_rows = [row for row, _profile in rows]
    rating_values = [row.rating for row in engagement_rows if row.rating]
    own = None
    if profile:
        own = next((row for row in engagement_rows if row.user_id == profile.id), None)

    return {
        "book_id": book_id,
        "average_rating": round(sum(rating_values) / len(rating_values), 1) if rating_values else None,
        "rating_count": len(rating_values),
        "like_count": sum(1 for row in engagement_rows if row.liked),
        "comment_count": sum(
            1
            for row in engagement_rows
            if row.comment and row.comment_status == "approved"
        ),
        "viewer": {
            "rating": own.rating if own else None,
            "liked": bool(own.liked) if own else False,
            "comment_status": own.comment_status if own and own.comment else None,
            "comment_id": own.id if own and own.comment else None,
        },
        "comments": [
            {
                "id": row.id,
                "reader": reader.username if reader and reader.username else "PickBook Reader",
                "mine": bool(profile and row.user_id == profile.id),
                "rating": row.rating,
                "comment": row.comment,
                "created_at": row.created_at,
                "edited_at": row.edited_at,
            }
            for row, reader in rows
            if row.comment and row.comment_status == "approved"
        ][:20],
    }


def _story_for_book(db, book_id: int) -> Story | None:
    return db.query(Story).filter(
        Story.published_version_id == book_id,
        Story.author_id.isnot(None),
    ).first()


def _post_author_earning(
    db,
    book_id: int,
    source: str,
    amount_kobo: int,
    note: str,
) -> None:
    if amount_kobo <= 0:
        return
    story = _story_for_book(db, book_id)
    if not story or not story.author_id:
        return
    db.add(
        AuthorEarning(
            author_id=story.author_id,
            story_id=story.id,
            source=source,
            amount_kobo=amount_kobo,
            note=note[:1000],
        )
    )


def _achievement_payload(row: ReaderAchievement) -> dict:
    return {
        "code": row.code,
        "title": row.title,
        "description": row.description,
        "progress": row.progress,
        "target": row.target,
        "earned": bool(row.earned_at),
        "earned_at": row.earned_at,
    }


def _sync_achievements(db, profile: Profile) -> list[ReaderAchievement]:
    today = datetime.now(timezone.utc)
    progress_rows = db.query(ReadingProgress).filter(ReadingProgress.user_id == profile.id).all()
    downloads = db.query(Download).filter(Download.user_id == profile.id).count()
    engagement_count = db.query(ReaderEngagement).filter(
        ReaderEngagement.user_id == profile.id,
    ).count()
    premium_reads = 1 if profile.current_plan == "standard" else 0
    values = {
        "read_10_books": len({row.book_id for row in progress_rows if row.book_id}),
        "first_premium_read": premium_reads,
        "streak_30": int(profile.streak_count or 0),
        "top_supporter": engagement_count,
        "premium_explorer": downloads,
    }
    rows = []
    for code, (title, description, target) in ACHIEVEMENT_DEFS.items():
        row = db.query(ReaderAchievement).filter(
            ReaderAchievement.user_id == profile.id,
            ReaderAchievement.code == code,
        ).first()
        if not row:
            row = ReaderAchievement(
                user_id=profile.id,
                code=code,
                title=title,
                description=description,
                target=target,
            )
            db.add(row)
        row.progress = min(int(values.get(code, 0)), target)
        row.updated_at = today
        if row.progress >= target and not row.earned_at:
            row.earned_at = today
        rows.append(row)
    db.commit()
    return rows


@router.get("/novels/{book_id}/download")
@limiter.limit("20/hour")
def download_novel(
    book_id: int,
    request: Request,
    profile: Profile = Depends(require_standard_profile),
    db=Depends(get_db),
):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found.")

    chapters = _chapter_list(book)
    if not chapters:
        raise HTTPException(
            status_code=404,
            detail="No cached chapters are available for offline download.",
        )

    existing_download = db.query(Download).filter(
        Download.user_id == profile.id,
        Download.book_id == book.id,
        Download.story_id.is_(None),
    ).first()
    if not existing_download:
        db.add(Download(user_id=profile.id, book_id=book.id))
        _post_author_earning(
            db,
            book.id,
            "premium_download",
            50,
            f"Premium download by reader {profile.id}.",
        )
        db.commit()
        _sync_achievements(db, profile)

    return {
        "book": {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "genre": book.genre,
            "source": book.source,
            "cover": book.cover,
            "synopsis": book.synopsis,
            "download": book.download,
        },
        "downloaded_by": profile.id,
        "chapters": chapters,
        "chapter_count": len(chapters),
    }


@router.get("/novels/{book_id}/engagement")
def get_book_engagement(book_id: int, db=Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found.")
    return _engagement_summary(db, book_id)


@router.get("/novels/{book_id}/engagement/me")
def get_own_book_engagement(
    book_id: int,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found.")
    return _engagement_summary(db, book_id, profile)


@router.post("/novels/{book_id}/engagement")
@limiter.limit("20/minute")
def save_book_engagement(
    book_id: int,
    payload: EngagementPayload,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found.")
    if payload.rating is not None and payload.rating not in {1, 2, 3, 4, 5}:
        raise HTTPException(status_code=400, detail="Rating must be 1 to 5.")

    engagement = db.query(ReaderEngagement).filter(
        ReaderEngagement.user_id == profile.id,
        ReaderEngagement.book_id == book_id,
    ).first()
    if not engagement:
        engagement = ReaderEngagement(user_id=profile.id, book_id=book_id)
        db.add(engagement)

    if payload.rating is not None:
        engagement.rating = payload.rating
    if payload.liked is not None:
        engagement.liked = 1 if payload.liked else 0
    if payload.comment is not None:
        engagement.comment = _clean_comment(payload.comment)
        engagement.comment_status = "pending_review" if engagement.comment else "approved"
        engagement.edited_at = datetime.now(timezone.utc) if engagement.id else None
    engagement.updated_at = datetime.now(timezone.utc)
    db.add(engagement)
    db.commit()
    _sync_achievements(db, profile)
    return _engagement_summary(db, book_id, profile)


@router.put("/novels/{book_id}/engagement/comment")
@limiter.limit("20/minute")
def edit_book_comment(
    book_id: int,
    payload: EngagementPayload,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    engagement = db.query(ReaderEngagement).filter(
        ReaderEngagement.user_id == profile.id,
        ReaderEngagement.book_id == book_id,
    ).first()
    if not engagement or not engagement.comment:
        raise HTTPException(status_code=404, detail="Comment not found.")
    engagement.comment = _clean_comment(payload.comment)
    engagement.comment_status = "pending_review" if engagement.comment else "approved"
    engagement.edited_at = datetime.now(timezone.utc)
    engagement.updated_at = engagement.edited_at
    db.add(engagement)
    db.commit()
    return _engagement_summary(db, book_id, profile)


@router.delete("/novels/{book_id}/engagement/comment")
@limiter.limit("20/minute")
def delete_book_comment(
    book_id: int,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    engagement = db.query(ReaderEngagement).filter(
        ReaderEngagement.user_id == profile.id,
        ReaderEngagement.book_id == book_id,
    ).first()
    if not engagement or not engagement.comment:
        raise HTTPException(status_code=404, detail="Comment not found.")
    engagement.comment = None
    engagement.comment_status = "approved"
    engagement.moderation_note = None
    engagement.report_reason = None
    engagement.updated_at = datetime.now(timezone.utc)
    db.add(engagement)
    db.commit()
    return _engagement_summary(db, book_id, profile)


@router.post("/engagement/comments/{engagement_id}/report")
@limiter.limit("10/hour")
def report_comment(
    engagement_id: int,
    payload: CommentReportPayload,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    engagement = db.query(ReaderEngagement).filter(ReaderEngagement.id == engagement_id).first()
    if not engagement or not engagement.comment:
        raise HTTPException(status_code=404, detail="Comment not found.")
    if engagement.user_id == profile.id:
        raise HTTPException(status_code=400, detail="You cannot report your own comment.")
    engagement.report_count = int(engagement.report_count or 0) + 1
    reason = bleach.clean(payload.reason or "", tags=[], strip=True).strip()
    if reason:
        engagement.report_reason = reason[:2000]
    engagement.comment_status = "reported"
    engagement.updated_at = datetime.now(timezone.utc)
    db.add(engagement)
    db.commit()
    return {"status": "reported"}


@router.get("/authors/{author_id}/profile")
def public_author_profile(
    author_id: str,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
    db=Depends(get_db),
):
    profile = db.query(Profile).filter(Profile.id == author_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Author not found.")
    stories = db.query(Story).filter(
        Story.author_id == author_id,
        Story.status == "published",
    ).all()
    story_ids = [story.id for story in stories]
    follows = db.query(AuthorFollow).filter(AuthorFollow.author_id == author_id).count()
    ratings = []
    likes = 0
    if story_ids:
        rows = db.query(ReaderEngagement).filter(ReaderEngagement.story_id.in_(story_ids)).all()
        ratings = [row.rating for row in rows if row.rating]
        likes = sum(1 for row in rows if row.liked)
    viewer_id = None
    if authorization and authorization.lower().startswith("bearer "):
        viewer_id = profile_id_from_token(authorization.split(" ", 1)[1].strip())
    if not viewer_id:
        viewer_id = x_user_id

    is_following = False
    if viewer_id:
        is_following = db.query(AuthorFollow).filter(
            AuthorFollow.reader_id == viewer_id,
            AuthorFollow.author_id == author_id,
        ).first() is not None

    return {
        "id": profile.id,
        "name": profile.username or "PickBook Author",
        "bio": profile.author_bio,
        "verified": bool(profile.email_verified),
        "followers": follows,
        "viewer_following": is_following,
        "following": db.query(AuthorFollow).filter(AuthorFollow.reader_id == author_id).count(),
        "published_books": len(stories),
        "total_reads": 0,
        "average_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
        "total_tips_received_naira": 0,
        "premium_reading_earnings_naira": 0,
        "premium_download_earnings_naira": 0,
        "likes": likes,
        "catalog": [
            {
                "id": story.id,
                "book_id": story.published_version_id,
                "title": story.title,
                "cover": story.cover,
                "synopsis": story.synopsis,
                "published_at": story.published_at,
            }
            for story in stories
        ],
    }


@router.post("/authors/{author_id}/follow")
@limiter.limit("30/minute")
def follow_author(
    author_id: str,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    if author_id == profile.id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself.")
    author = db.query(Profile).filter(Profile.id == author_id).first()
    if not author:
        raise HTTPException(status_code=404, detail="Author not found.")
    existing = db.query(AuthorFollow).filter(
        AuthorFollow.reader_id == profile.id,
        AuthorFollow.author_id == author_id,
    ).first()
    if not existing:
        db.add(AuthorFollow(reader_id=profile.id, author_id=author_id))
        db.commit()
    return {"status": "following", "author_id": author_id}


@router.delete("/authors/{author_id}/follow")
@limiter.limit("30/minute")
def unfollow_author(
    author_id: str,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    db.query(AuthorFollow).filter(
        AuthorFollow.reader_id == profile.id,
        AuthorFollow.author_id == author_id,
    ).delete(synchronize_session=False)
    db.commit()
    return {"status": "unfollowed", "author_id": author_id}


@router.get("/achievements")
def reader_achievements(
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    rows = _sync_achievements(db, profile)
    return [_achievement_payload(row) for row in rows]


@router.get("/reader-state")
def get_reader_state(
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    return _reader_state_response(profile, db)


@router.post("/reader-state")
@limiter.limit("20/minute")
def sync_reader_state(
    payload: ReaderStatePayload,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    shelf_ids = {int(book_id) for book_id in payload.shelf if int(book_id) > 0}
    progress_ids = {
        int(book_id)
        for book_id in payload.progress.keys()
        if str(book_id).isdigit() and int(book_id) > 0
    }
    bookmark_ids = set()
    for key, enabled in payload.bookmarks.items():
        if not enabled:
            continue
        parts = _bookmark_parts(key)
        if parts:
            bookmark_ids.add(parts[0])
    valid_ids = _book_ids(db, shelf_ids | progress_ids | bookmark_ids)

    _replace_user_book_rows(db, UserLibraryItem, profile.id)
    _replace_user_book_rows(db, ReadingProgress, profile.id)
    _replace_user_book_rows(db, Bookmark, profile.id)

    for book_id in sorted(shelf_ids & valid_ids):
        db.add(UserLibraryItem(user_id=profile.id, book_id=book_id))

    for book_id_text, progress in payload.progress.items():
        if not str(book_id_text).isdigit():
            continue
        book_id = int(book_id_text)
        if book_id not in valid_ids:
            continue
        db.add(
            ReadingProgress(
                user_id=profile.id,
                book_id=book_id,
                chapter=max(1, int(progress.chapter or 1)),
                percent=_bounded_percent(progress.percent if progress.percent is not None else progress.pct),
            )
        )

    for key, enabled in payload.bookmarks.items():
        if not enabled:
            continue
        parts = _bookmark_parts(key)
        if not parts:
            continue
        book_id, chapter = parts
        if book_id not in valid_ids:
            continue
        db.add(
            Bookmark(
                user_id=profile.id,
                book_id=book_id,
                chapter=max(1, chapter),
            )
        )

    db.commit()
    _sync_achievements(db, profile)
    return _reader_state_response(profile, db)


@router.post("/streak/ping")
@limiter.limit("10/minute")
def streak_ping(
    payload: StreakPayload,
    request: Request,
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    managed_profile = db.query(Profile).filter(Profile.id == profile.id).first()
    if not managed_profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    today = date.today()
    yesterday = today - timedelta(days=1)
    last_read = managed_profile.last_read_date

    if last_read == today:
        changed = False
    elif last_read == yesterday:
        managed_profile.streak_count += 1
        managed_profile.last_read_date = today
        changed = True
    else:
        managed_profile.streak_count = 1
        managed_profile.last_read_date = today
        changed = True

    activity = (
        db.query(ReadingActivity)
        .filter(
            ReadingActivity.user_id == managed_profile.id,
            ReadingActivity.read_date == today,
        )
        .first()
    )
    if activity:
        activity.chapters_read += 1
    else:
        db.add(ReadingActivity(user_id=managed_profile.id, read_date=today))

    milestone = None
    if changed and managed_profile.streak_count in (3, 7, 10, 30, 60, 90):
        milestone = managed_profile.streak_count
    if payload.book_id:
        _post_author_earning(
            db,
            int(payload.book_id),
            "premium_reading",
            25 if managed_profile.current_plan == "standard" else 5,
            f"Reader activity by {managed_profile.id}.",
        )

    try:
        db.add(managed_profile)
        db.commit()
        _sync_achievements(db, managed_profile)
    except IntegrityError:
        db.rollback()

    return {
        "streak_count": managed_profile.streak_count,
        "last_read_date": managed_profile.last_read_date,
        "milestone": milestone,
    }


@router.get("/streak/calendar")
def streak_calendar(
    profile: Profile = Depends(get_current_profile),
    db=Depends(get_db),
):
    since = date.today() - timedelta(days=89)
    rows = (
        db.query(ReadingActivity)
        .filter(
            ReadingActivity.user_id == profile.id,
            ReadingActivity.read_date >= since,
        )
        .order_by(ReadingActivity.read_date.asc())
        .all()
    )

    return [
        {
            "date": row.read_date.isoformat(),
            "chapters_read": row.chapters_read,
        }
        for row in rows
    ]
