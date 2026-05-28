import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.core.auth import get_current_profile, require_standard_profile
from app.core.database import SessionLocal
from app.core.limiter import limiter
from app.models.book import Book, Profile, ReadingActivity

router = APIRouter(prefix="/api", tags=["Features"])


class StreakPayload(BaseModel):
    chapter: int | None = None
    book_id: int | None = None


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

    try:
        db.add(managed_profile)
        db.commit()
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
