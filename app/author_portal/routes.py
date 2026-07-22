from pathlib import Path, PurePath, PureWindowsPath
from datetime import datetime, timezone
from dataclasses import dataclass
import json
import re
import secrets
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, UploadFile, File, Form, Header, HTTPException, Request, Response
from pydantic import BaseModel
import bleach
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.auth import create_access_token, profile_id_from_token
from app.core.chapter_text import (
    CHAPTER_ALLOWED_EXTENSIONS,
    CHAPTER_FILENAME_FORMAT,
    ChapterExtractionError,
    chapter_title_from_filename,
    extract_chapter_text,
    parse_chapter_filename,
    word_count,
)
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.limiter import limiter
from app.core.storage import (
    delete_object_url,
    save_local_file,
    upload_object,
    using_supabase_storage,
)
from app.models.book import (
    AppSetting,
    AuthorApplication,
    AuthorEarning,
    AuthorNotification,
    Book,
    Chapter,
    Draft,
    PremiumRead,
    Profile,
    Story,
    StoryReviewAudit,
    WithdrawalRequest,
)

router = APIRouter(
    prefix="/author",
    tags=["Author Portal"],
)

ALLOWED_EXTENSIONS = {".txt", ".docx", ".epub"}
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_STORED_TEXT_BYTES = 2 * 1024 * 1024  # Keep large uploads as metadata-only.
ALLOWED_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_COVER_SIZE_BYTES = 5 * 1024 * 1024
TERMS_VERSION = "author-agreement-monetization-2026-07-10"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
COVER_UPLOAD_DIR = PROJECT_ROOT / "public" / "uploads" / "covers"
MINIMUM_PAYOUT_KEY = "minimum_author_payout_naira"


class SubmissionResponse(BaseModel):
    status: str
    title: str
    author: str
    filename: str
    cover: str | None = None
    author_id: str
    author_access_token: str
    story_id: int
    draft_id: int
    manuscript: dict


class AuthorSubmissionItem(BaseModel):
    story_id: int
    draft_id: int | None
    title: str
    status: str
    review_feedback: str | None = None
    cover: str | None = None
    original_status: str = "standard"
    filename: str | None = None
    extension: str | None = None
    size_bytes: int | None = None
    stored_text: bool = False
    submitted_at: str | None = None
    updated_at: str | None = None


class AuthorDashboardResponse(BaseModel):
    author_id: str
    author_access_token: str | None = None
    author_name: str | None = None
    submissions: list[AuthorSubmissionItem]
    earnings: dict | None = None
    withdrawals: list[dict] = []
    notifications: list[dict] = []
    minimum_payout_naira: int = 1000
    application_status: str | None = None
    premium_analytics: dict | None = None


class StoryUpdatePayload(BaseModel):
    title: str | None = None
    synopsis: str | None = None
    content: str | None = None
    original_status: str | None = None


class AuthorApplicationPayload(BaseModel):
    pen_name: str
    target_genres: list[str] = []
    short_bio: str | None = None
    writing_sample_url: str | None = None


class BankDetailsPayload(BaseModel):
    bank_name: str
    bank_account_name: str
    bank_account_number: str


class WithdrawalPayload(BaseModel):
    amount_naira: int


class ChapterOut(BaseModel):
    id: int
    story_id: int
    title: str
    status: str
    position: int
    word_count: int
    original_filename: str | None = None
    upload_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    published_at: str | None = None


class ChapterDetailOut(ChapterOut):
    content: str | None = None


class ChapterListResponse(BaseModel):
    story_id: int
    chapters: list[ChapterOut]
    total_chapters: int
    published_chapters: int
    total_words: int


class ChapterUpdatePayload(BaseModel):
    title: str | None = None
    content: str | None = None
    status: str | None = None


class ChapterReorderPayload(BaseModel):
    chapter_ids: list[int]


class ChapterBulkUploadItem(BaseModel):
    filename: str
    status: str
    chapter_id: int | None = None
    title: str | None = None
    word_count: int | None = None
    error: str | None = None


class ChapterBulkUploadResponse(BaseModel):
    story_id: int
    uploaded: int
    failed: int
    results: list[ChapterBulkUploadItem]


@dataclass
class PreparedChapterUpload:
    upload_index: int
    upload: UploadFile
    filename: str
    chapter_number: int
    parsed_title: str | None = None


def _safe_text(value: str) -> str:
    """Strip all HTML tags and limit length."""
    return bleach.clean(value, tags=[], strip=True)[:500]


def _safe_long_text(value: str | None, limit: int = 200_000) -> str | None:
    if value is None:
        return None
    return bleach.clean(value, tags=[], strip=True)[:limit]


def _clean_original_status(value: str | None) -> str:
    status = (value or "standard").strip().lower().replace("-", "_")
    if status in {"pickbook_original", "original", "exclusive"}:
        return "pickbook_original"
    if status == "standard":
        return "standard"
    raise HTTPException(status_code=400, detail="Original status must be standard or pickbook_original.")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "story"


def _local_author_profile_id(author_name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"pickbook-author:{author_name.lower()}"))


def _local_author_email(author_name: str) -> str:
    slug = _slugify(author_name)[:64]
    return f"author+{slug}@pickbook.local"


def _profile_id_from_request(
    authorization: str | None,
    x_user_id: str | None,
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return profile_id_from_token(authorization.split(" ", 1)[1].strip())
    if settings.app_env != "production":
        return x_user_id
    return None


def _available_username(db, author_name: str, profile_id: str) -> str | None:
    username = author_name[:150]
    existing = db.query(Profile.id).filter(Profile.username == username).first()
    if existing and existing[0] != profile_id:
        return None
    return username


def _find_or_create_author_profile(
    db,
    author_name: str,
    authorization: str | None,
    x_user_id: str | None,
) -> Profile:
    requested_profile_id = _profile_id_from_request(authorization, x_user_id)
    if requested_profile_id:
        profile = db.query(Profile).filter(Profile.id == requested_profile_id).first()
        if profile:
            if not profile.username:
                profile.username = _available_username(
                    db,
                    author_name,
                    requested_profile_id,
                )
            return profile

        profile = Profile(
            id=requested_profile_id,
            email=f"user+{_slugify(requested_profile_id)[:64]}@pickbook.local",
            username=_available_username(db, author_name, requested_profile_id),
        )
        db.add(profile)
        db.flush()
        return profile

    profile_id = _local_author_profile_id(author_name)
    profile = db.query(Profile).filter(Profile.id == profile_id).first()
    if profile:
        return profile

    profile = Profile(
        id=profile_id,
        email=_local_author_email(author_name),
        username=_available_username(db, author_name, profile_id),
    )
    db.add(profile)
    db.flush()
    return profile


def _unique_story_slug(db, title: str) -> str:
    base = _slugify(title)
    slug = base
    suffix = 2

    while db.query(Story.id).filter(Story.slug == slug).first():
        slug = f"{base[:72]}-{suffix}"
        suffix += 1

    return slug


def _manuscript_metadata(draft: Draft | None) -> dict:
    if not draft or not draft.manuscript_metadata_json:
        return {}
    try:
        data = json.loads(draft.manuscript_metadata_json)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_cover_filename(story_id: int, filename: str) -> str:
    suffix = PurePath(filename or "").suffix.lower()
    return f"story-{story_id}-{secrets.token_hex(8)}{suffix}"


async def _store_cover(story: Story, cover: UploadFile | None) -> str | None:
    if not cover or not cover.filename:
        return None

    original_name = PureWindowsPath(cover.filename).name
    suffix = PurePath(original_name).suffix.lower()
    if suffix not in ALLOWED_COVER_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Cover must be JPG, PNG, or WEBP.",
        )

    content = await cover.read(MAX_COVER_SIZE_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Cover image is empty.")
    if len(content) > MAX_COVER_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Cover image must be 5 MB or smaller.")

    content_type = (cover.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Cover upload must be an image.")

    stored = _safe_cover_filename(story.id, original_name)

    if using_supabase_storage():
        story.cover = upload_object(
            bucket=settings.supabase_public_bucket,
            object_path=f"covers/{stored}",
            content=content,
            content_type=cover.content_type or "image/jpeg",
            public=True,
        )
        return story.cover

    save_local_file(COVER_UPLOAD_DIR, stored, content)
    story.cover = f"/uploads/covers/{stored}"
    return story.cover


def _delete_local_cover(cover_url: str | None) -> None:
    if using_supabase_storage():
        delete_object_url(cover_url)
        return

    if not cover_url or not cover_url.startswith("/uploads/covers/"):
        return
    cover_path = (PROJECT_ROOT / "public" / cover_url.lstrip("/")).resolve()
    if COVER_UPLOAD_DIR.resolve() not in cover_path.parents:
        return
    try:
        cover_path.unlink(missing_ok=True)
    except OSError:
        pass


def _author_totals(db, author_id: str) -> dict:
    earnings = db.query(AuthorEarning).filter(AuthorEarning.author_id == author_id).all()
    withdrawals = db.query(WithdrawalRequest).filter(WithdrawalRequest.author_id == author_id).all()
    earned = sum(row.amount_kobo for row in earnings if row.status in {"available", "paid"})
    pending_withdrawals = sum(row.amount_kobo for row in withdrawals if row.status == "pending")
    paid_withdrawals = sum(row.amount_kobo for row in withdrawals if row.status == "paid")
    return {
        "earned_naira": earned / 100,
        "pending_withdrawals_naira": pending_withdrawals / 100,
        "paid_withdrawals_naira": paid_withdrawals / 100,
        "available_naira": max(0, earned - pending_withdrawals - paid_withdrawals) / 100,
    }


def _minimum_payout_naira(db) -> int:
    row = db.query(AppSetting).filter(AppSetting.key == MINIMUM_PAYOUT_KEY).first()
    try:
        return max(1, int(row.value)) if row and row.value is not None else 1000
    except (TypeError, ValueError):
        return 1000


def _notify_author(db, story: Story, kind: str, title: str, message: str | None) -> None:
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


def _author_application_status(profile: Profile) -> str | None:
    return profile.author_application_status or (
        "approved" if getattr(profile, "role", "reader") == "author" else None
    )


def _require_approved_author(profile: Profile) -> None:
    if getattr(profile, "role", "reader") != "author" and _author_application_status(profile) != "approved":
        raise HTTPException(
            status_code=403,
            detail="Your author application is under review! Check back soon to see if your account has been activated.",
        )


def _premium_analytics(db, author_id: str) -> dict:
    rows = (
        db.query(PremiumRead, Book, Story)
        .outerjoin(Book, PremiumRead.book_id == Book.id)
        .outerjoin(Story, PremiumRead.story_id == Story.id)
        .filter(PremiumRead.author_id == author_id)
        .all()
    )
    unique_readers = {row.user_id for row, _book, _story in rows}
    by_book = {}
    for row, book, story in rows:
        key = row.book_id
        item = by_book.setdefault(
            key,
            {
                "book_id": row.book_id,
                "story_id": row.story_id,
                "title": (book.title if book else None) or (story.title if story else "Untitled"),
                "premium_readers": set(),
            },
        )
        item["premium_readers"].add(row.user_id)
    books = [
        {
            **{key: value for key, value in item.items() if key != "premium_readers"},
            "premium_readers": len(item["premium_readers"]),
        }
        for item in by_book.values()
    ]
    books.sort(key=lambda item: (-item["premium_readers"], item["title"]))
    return {
        "unique_premium_readers": len(unique_readers),
        "total_premium_book_reads": len(rows),
        "books": books,
    }


def _audit_story(db, story: Story, action: str, from_status: str | None, to_status: str | None, note: str | None = None) -> None:
    db.add(
        StoryReviewAudit(
            story_id=story.id,
            admin_id="author",
            action=action,
            from_status=from_status,
            to_status=to_status,
            note=(note or "")[:5000] or None,
        )
    )


@router.post("/application")
@limiter.limit("5/hour")
def submit_author_application(
    payload: AuthorApplicationPayload,
    request: Request,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        pen_name = _safe_text(payload.pen_name)
        if not pen_name:
            raise HTTPException(status_code=400, detail="Pen name is required.")
        genres = [
            _safe_text(genre)
            for genre in payload.target_genres[:8]
            if _safe_text(genre)
        ]
        existing = (
            db.query(AuthorApplication)
            .filter(AuthorApplication.profile_id == profile.id)
            .order_by(AuthorApplication.id.desc())
            .first()
        )
        if _author_application_status(profile) == "approved":
            return {"status": "approved", "message": "Your author account is active."}
        if existing and existing.status == "approved":
            return {"status": "approved", "message": "Your author account is active."}
        application = existing if existing and existing.status == "pending" else AuthorApplication(profile_id=profile.id)
        application.pen_name = pen_name
        application.target_genres = json.dumps(genres)
        application.short_bio = _safe_long_text(payload.short_bio, 1600)
        application.writing_sample_url = _safe_text(payload.writing_sample_url or "") or None
        application.status = "pending"
        application.admin_feedback = None
        application.submitted_at = datetime.now(timezone.utc)
        profile.username = profile.username or _available_username(db, pen_name, profile.id)
        profile.author_bio = application.short_bio
        profile.author_application_status = "pending"
        db.add(application)
        db.add(profile)
        db.commit()
        return {
            "status": "pending",
            "message": "Your author application is under review! Check back soon to see if your account has been activated.",
        }
    finally:
        db.close()


@router.get("/application")
def get_author_application(
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        application = (
            db.query(AuthorApplication)
            .filter(AuthorApplication.profile_id == profile.id)
            .order_by(AuthorApplication.id.desc())
            .first()
        )
        return {
            "status": _author_application_status(profile) or (application.status if application else None),
            "role": getattr(profile, "role", "reader"),
            "pen_name": application.pen_name if application else profile.username,
            "target_genres": json.loads(application.target_genres or "[]") if application and application.target_genres else [],
            "short_bio": application.short_bio if application else profile.author_bio,
            "writing_sample_url": application.writing_sample_url if application else None,
            "admin_feedback": application.admin_feedback if application else None,
        }
    finally:
        db.close()


@router.post("/submit", response_model=SubmissionResponse)
@limiter.limit("10/minute")
async def submit_novel(
    request: Request,
    title: str = Form(..., min_length=1, max_length=500),
    author: str = Form(..., min_length=1, max_length=200),
    genre: str | None = Form(None),
    tags: str | None = Form(None),
    consent: bool = Form(...),
    terms_accepted: bool = Form(False),
    original_status: str = Form("standard"),
    file: UploadFile | None = File(None),
    cover: UploadFile | None = File(None),
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    if not consent:
        raise HTTPException(status_code=400, detail="Consent required.")
    if not terms_accepted:
        raise HTTPException(
            status_code=400,
            detail="Author agreement acceptance required.",
        )

    original_name = PureWindowsPath(file.filename or "").name if file and file.filename else ""
    original_name = PurePath(original_name).name if original_name else ""

    if original_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    if original_name and "." not in original_name.strip("."):
        raise HTTPException(status_code=400, detail="File must have an extension.")

    extension = "." + original_name.rsplit(".", 1)[-1].lower() if original_name else ""
    if extension not in ALLOWED_EXTENSIONS:
        if original_name:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{extension}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            )

    # Enforce file size limit — read only the header chunk needed for the check.
    chunk = await file.read(MAX_FILE_SIZE_BYTES + 1) if file and original_name else b""
    if len(chunk) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB limit.",
        )

    # Sanitize free-text inputs before storing / echoing back.
    safe_title = _safe_text(title)
    safe_author = _safe_text(author)
    safe_genre = _safe_text(genre or "") or "Original"
    safe_tags = _safe_text(tags or "")
    safe_original_status = _clean_original_status(original_status)
    if not safe_title:
        raise HTTPException(status_code=400, detail="Title is required.")
    if not safe_author:
        raise HTTPException(status_code=400, detail="Author is required.")

    now = datetime.now(timezone.utc)
    manuscript_metadata = {
        "original_filename": original_name or None,
        "extension": extension or None,
        "content_type": file.content_type if file else None,
        "size_bytes": len(chunk),
        "stored_text": extension == ".txt" and len(chunk) <= MAX_STORED_TEXT_BYTES,
        "genre": safe_genre,
        "tags": safe_tags,
        "submitted_at": now.isoformat(),
    }
    draft_content = None
    if manuscript_metadata["stored_text"]:
        draft_content = chunk.decode("utf-8", errors="replace")

    db = SessionLocal()
    try:
        profile = _find_or_create_author_profile(
            db,
            safe_author,
            authorization,
            x_user_id,
        )
        _require_approved_author(profile)
        profile.terms_accepted_at = now
        profile.terms_version = TERMS_VERSION
        db.add(profile)
        story = Story(
            author_id=profile.id,
            title=safe_title,
            slug=_unique_story_slug(db, safe_title),
            genre=safe_genre,
            original_status=safe_original_status,
            status="pending_review",
            updated_at=now,
        )
        db.add(story)
        db.flush()
        cover_url = await _store_cover(story, cover)

        draft = Draft(
            story_id=story.id,
            author_id=profile.id,
            title=safe_title,
            content=draft_content,
            manuscript_metadata_json=json.dumps(manuscript_metadata, sort_keys=True),
            updated_at=now,
        )
        db.add(draft)
        db.commit()
        db.refresh(story)
        db.refresh(draft)
        author_id = profile.id
        story_id = story.id
        draft_id = draft.id
        story_cover = cover_url
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Could not create a unique author submission. Please try again.",
        ) from exc
    finally:
        db.close()

    return SubmissionResponse(
        status="pending_review",
        title=safe_title,
        author=safe_author,
        filename=original_name or "chapter-workflow",
        cover=story_cover,
        author_id=author_id,
        author_access_token=create_access_token(author_id),
        story_id=story_id,
        draft_id=draft_id,
        manuscript=manuscript_metadata,
    )


@router.get("/submissions", response_model=AuthorDashboardResponse)
def list_author_submissions(
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    author_id = _profile_id_from_request(authorization, x_user_id)
    if not author_id:
        raise HTTPException(
            status_code=401,
            detail="Submit a story or sign in to view your author dashboard.",
        )

    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.id == author_id).first()
        if not profile:
            raise HTTPException(status_code=404, detail="Author profile not found.")
        application_status = _author_application_status(profile)

        rows = (
            db.query(Story, Draft)
            .outerjoin(
                Draft,
                (Draft.story_id == Story.id) & (Draft.author_id == author_id),
            )
            .filter(Story.author_id == author_id)
            .order_by(Story.updated_at.desc(), Story.id.desc())
            .all()
        )

        submissions = []
        for story, draft in rows:
            metadata = _manuscript_metadata(draft)
            updated_at = draft.updated_at or story.updated_at or story.created_at
            submissions.append(
                AuthorSubmissionItem(
                    story_id=story.id,
                    draft_id=draft.id if draft else None,
                    title=draft.title if draft else story.title,
                    status=story.status,
                    review_feedback=story.review_feedback,
                    cover=story.cover,
                    original_status=story.original_status,
                    filename=metadata.get("original_filename"),
                    extension=metadata.get("extension"),
                    size_bytes=metadata.get("size_bytes"),
                    stored_text=bool(metadata.get("stored_text")),
                    submitted_at=metadata.get("submitted_at"),
                    updated_at=updated_at.isoformat() if updated_at else None,
                )
            )

        return AuthorDashboardResponse(
            author_id=profile.id,
            author_access_token=create_access_token(profile.id),
            author_name=profile.username,
            submissions=submissions,
            earnings=_author_totals(db, profile.id),
            application_status=application_status,
            premium_analytics=_premium_analytics(db, profile.id),
            minimum_payout_naira=_minimum_payout_naira(db),
            withdrawals=[
                {
                    "id": row.id,
                    "amount_naira": row.amount_kobo / 100,
                    "status": row.status,
                    "requested_at": row.requested_at.isoformat() if row.requested_at else None,
                    "processed_at": row.processed_at.isoformat() if row.processed_at else None,
                    "admin_note": row.admin_note,
                }
                for row in db.query(WithdrawalRequest)
                .filter(WithdrawalRequest.author_id == profile.id)
                .order_by(WithdrawalRequest.requested_at.desc(), WithdrawalRequest.id.desc())
                .limit(20)
                .all()
            ],
            notifications=[
                {
                    "id": row.id,
                    "story_id": row.story_id,
                    "kind": row.kind,
                    "title": row.title,
                    "message": row.message,
                    "read_at": row.read_at.isoformat() if row.read_at else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in db.query(AuthorNotification)
                .filter(AuthorNotification.author_id == profile.id)
                .order_by(AuthorNotification.created_at.desc(), AuthorNotification.id.desc())
                .limit(20)
                .all()
            ],
        )
    finally:
        db.close()


def _author_profile_or_401(db, authorization: str | None, x_user_id: str | None) -> Profile:
    author_id = _profile_id_from_request(authorization, x_user_id)
    if not author_id:
        raise HTTPException(status_code=401, detail="Sign in required.")
    profile = db.query(Profile).filter(Profile.id == author_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Author profile not found.")
    return profile


def _author_story_or_404(db, author_id: str, story_id: int) -> tuple[Story, Draft | None]:
    story = db.query(Story).filter(Story.id == story_id, Story.author_id == author_id).first()
    if not story:
        raise HTTPException(status_code=404, detail="Story not found.")
    draft = db.query(Draft).filter(Draft.story_id == story.id, Draft.author_id == author_id).first()
    return story, draft


def _author_chapter_or_404(db, story_id: int, chapter_id: int) -> Chapter:
    chapter = (
        db.query(Chapter)
        .filter(Chapter.id == chapter_id, Chapter.story_id == story_id)
        .first()
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found.")
    return chapter


def _next_chapter_position(db, story_id: int) -> int:
    current_max = (
        db.query(func.max(Chapter.position))
        .filter(Chapter.story_id == story_id)
        .scalar()
    )
    return (current_max or 0) + 1


def _chapter_out(chapter: Chapter, include_content: bool = False) -> dict:
    data = {
        "id": chapter.id,
        "story_id": chapter.story_id,
        "title": chapter.title,
        "status": chapter.status,
        "position": chapter.position,
        "word_count": chapter.word_count,
        "original_filename": chapter.original_filename,
        "upload_error": chapter.upload_error,
        "created_at": chapter.created_at.isoformat() if chapter.created_at else None,
        "updated_at": chapter.updated_at.isoformat() if chapter.updated_at else None,
        "published_at": chapter.published_at.isoformat() if chapter.published_at else None,
    }
    if include_content:
        data["content"] = chapter.content
    return data


def _clean_chapter_filename(raw_filename: str | None) -> str:
    """Strip any client-supplied path info down to a plain file name."""
    name = PureWindowsPath(raw_filename or "").name
    name = PurePath(name).name
    return name


def _chapter_extension(filename: str) -> str:
    if "." not in filename.strip("."):
        raise HTTPException(status_code=400, detail=f"'{filename}' must have a file extension.")
    return "." + filename.rsplit(".", 1)[-1].lower()


def _parse_chapter_upload_filename(filename: str) -> tuple[int, str | None]:
    try:
        return parse_chapter_filename(filename)
    except ChapterExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _prepare_chapter_uploads(files: list[UploadFile]) -> list[PreparedChapterUpload]:
    prepared: list[PreparedChapterUpload] = []
    seen: dict[int, str] = {}

    for index, upload in enumerate(files):
        original_name = _clean_chapter_filename(upload.filename)
        if not original_name or original_name in {".", ".."}:
            raise HTTPException(status_code=400, detail="Invalid filename.")

        extension = _chapter_extension(original_name)
        if extension not in CHAPTER_ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported file type '{extension}' for '{original_name}'. "
                    f"Allowed: {', '.join(sorted(CHAPTER_ALLOWED_EXTENSIONS))}"
                ),
            )

        chapter_number, parsed_title = _parse_chapter_upload_filename(original_name)
        if chapter_number in seen:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Chapter number {chapter_number} appears in both "
                    f"'{seen[chapter_number]}' and '{original_name}'. "
                    "Each uploaded chapter file must use a unique chapter number."
                ),
            )
        seen[chapter_number] = original_name
        prepared.append(
            PreparedChapterUpload(
                upload_index=index,
                upload=upload,
                filename=original_name,
                chapter_number=chapter_number,
                parsed_title=parsed_title,
            )
        )

    return sorted(prepared, key=lambda item: (item.chapter_number, item.upload_index))


def _existing_chapter_filename_numbers(db, story_id: int) -> dict[int, str]:
    rows = (
        db.query(Chapter.original_filename)
        .filter(Chapter.story_id == story_id, Chapter.original_filename.isnot(None))
        .all()
    )
    numbers: dict[int, str] = {}
    for (filename,) in rows:
        if not filename:
            continue
        try:
            chapter_number, _title = parse_chapter_filename(filename)
        except ChapterExtractionError:
            continue
        numbers.setdefault(chapter_number, filename)
    return numbers


def _reject_existing_chapter_number_conflicts(db, story_id: int, prepared: list[PreparedChapterUpload]) -> None:
    existing = _existing_chapter_filename_numbers(db, story_id)
    for item in prepared:
        existing_filename = existing.get(item.chapter_number)
        if existing_filename:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Chapter number {item.chapter_number} already exists in "
                    f"'{existing_filename}'. Rename '{item.filename}' or delete/reorder the existing chapter first."
                ),
            )


async def _read_chapter_upload(upload: UploadFile, original_name: str) -> bytes:
    chunk = await upload.read(MAX_FILE_SIZE_BYTES + 1)
    if len(chunk) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"'{original_name}' exceeds the {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB limit.",
        )
    if not chunk:
        raise HTTPException(status_code=400, detail=f"'{original_name}' is empty.")
    return chunk


async def _build_chapter_from_file(
    db,
    story: Story,
    profile: Profile,
    upload: UploadFile,
    *,
    position: int,
    title_override: str | None = None,
    original_name_override: str | None = None,
    parsed_title: str | None = None,
) -> Chapter:
    original_name = original_name_override or _clean_chapter_filename(upload.filename)
    if not original_name or original_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    extension = _chapter_extension(original_name)
    if extension not in CHAPTER_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{extension}'. "
                f"Allowed: {', '.join(sorted(CHAPTER_ALLOWED_EXTENSIONS))}"
            ),
        )

    chunk = await _read_chapter_upload(upload, original_name)

    try:
        text = extract_chapter_text(original_name, extension, chunk)
    except ChapterExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    safe_title = _safe_text(title_override or "") or _safe_text(parsed_title or "") or chapter_title_from_filename(original_name)
    chapter = Chapter(
        story_id=story.id,
        author_id=profile.id,
        title=safe_title,
        content=text,
        position=position,
        status="draft",
        word_count=word_count(text),
        original_filename=original_name,
        source_extension=extension,
    )
    db.add(chapter)
    db.flush()
    return chapter


@router.post("/stories/{story_id}/cover")
async def upload_story_cover(
    story_id: int,
    cover: UploadFile = File(...),
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        old_cover = story.cover
        cover_url = await _store_cover(story, cover)
        if old_cover and old_cover != cover_url:
            _delete_local_cover(old_cover)
        story.updated_at = datetime.now(timezone.utc)
        db.add(story)
        db.commit()
        return {"status": "updated", "cover": cover_url}
    finally:
        db.close()


@router.delete("/stories/{story_id}/cover")
def remove_story_cover(
    story_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        old_cover = story.cover
        story.cover = None
        story.updated_at = datetime.now(timezone.utc)
        db.add(story)
        db.commit()
        _delete_local_cover(old_cover)
        return {"status": "removed", "cover": None}
    finally:
        db.close()


@router.put("/stories/{story_id}")
def update_story(
    story_id: int,
    payload: StoryUpdatePayload,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, draft = _author_story_or_404(db, profile.id, story_id)
        previous_status = story.status
        if story.status in {"published", "rejected", "unpublished"}:
            story.status = "pending_review"
            story.review_feedback = "Updated by author and returned to review."
            _notify_author(
                db,
                story,
                "story_resubmitted",
                "Story returned to review",
                "Your story was edited and returned to review before readers see the new version.",
            )
        if payload.title is not None:
            safe_title = _safe_text(payload.title)
            if not safe_title:
                raise HTTPException(status_code=400, detail="Title is required.")
            story.title = safe_title
            if draft:
                draft.title = safe_title
        if payload.synopsis is not None:
            story.synopsis = _safe_long_text(payload.synopsis, 5000)
            if draft:
                draft.synopsis = story.synopsis
        if payload.original_status is not None:
            story.original_status = _clean_original_status(payload.original_status)
        if payload.content is not None:
            if not draft:
                draft = Draft(story_id=story.id, author_id=profile.id, title=story.title)
                db.add(draft)
            draft.content = _safe_long_text(payload.content)
        now = datetime.now(timezone.utc)
        story.updated_at = now
        if draft:
            draft.updated_at = now
            db.add(draft)
        if previous_status != story.status:
            _audit_story(db, story, "author_update", previous_status, story.status, story.review_feedback)
        db.add(story)
        db.commit()
        return {"status": "updated", "story_id": story.id, "story_status": story.status}
    finally:
        db.close()


@router.get("/stories/{story_id}/export")
def export_story(
    story_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, draft = _author_story_or_404(db, profile.id, story_id)
        content = draft.content if draft and draft.content else ""
        text = f"{story.title}\n\n{story.synopsis or ''}\n\n{content}".strip() + "\n"
        filename = f"{_slugify(story.title)}.txt"
        return Response(
            content=text,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        db.close()


@router.post("/stories/{story_id}/unpublish")
def unpublish_story(
    story_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        previous_status = story.status
        story.status = "unpublished"
        story.unpublished_at = datetime.now(timezone.utc)
        story.updated_at = story.unpublished_at
        _audit_story(db, story, "author_unpublish", previous_status, "unpublished", "Author unpublished this story.")
        _notify_author(
            db,
            story,
            "story_unpublished",
            "Story unpublished",
            "Your story is no longer visible to readers. Legal and payment records may be retained where required.",
        )
        db.add(story)
        db.commit()
        return {"status": "unpublished", "story_id": story.id}
    finally:
        db.close()


@router.delete("/stories/{story_id}")
def delete_story(
    story_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        db.add(
            AuthorNotification(
                author_id=profile.id,
                story_id=None,
                kind="story_deleted",
                title="Story deleted",
                message=(
                    f"Your story '{story.title}' has been deleted from your dashboard. "
                    "PickBook may retain limited legal, payment, and audit records where required."
                ),
            )
        )
        db.query(Draft).filter(Draft.story_id == story.id).delete(synchronize_session=False)
        db.query(AuthorEarning).filter(AuthorEarning.story_id == story.id).update(
            {"story_id": None},
            synchronize_session=False,
        )
        db.delete(story)
        db.commit()
        return {"status": "deleted", "story_id": story_id}
    finally:
        db.close()


@router.put("/bank-details")
def update_bank_details(
    payload: BankDetailsPayload,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        profile.bank_name = _safe_text(payload.bank_name)
        profile.bank_account_name = _safe_text(payload.bank_account_name)
        profile.bank_account_number = _safe_text(payload.bank_account_number)
        db.add(profile)
        db.commit()
        return {"status": "updated"}
    finally:
        db.close()


@router.get("/stories/{story_id}/chapters", response_model=ChapterListResponse)
def list_chapters(
    story_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        chapters = (
            db.query(Chapter)
            .filter(Chapter.story_id == story.id)
            .order_by(Chapter.position.asc(), Chapter.id.asc())
            .all()
        )
        return ChapterListResponse(
            story_id=story.id,
            chapters=[ChapterOut(**_chapter_out(chapter)) for chapter in chapters],
            total_chapters=len(chapters),
            published_chapters=sum(1 for chapter in chapters if chapter.status == "published"),
            total_words=sum(chapter.word_count or 0 for chapter in chapters),
        )
    finally:
        db.close()


@router.get("/stories/{story_id}/chapters/{chapter_id}", response_model=ChapterDetailOut)
def get_chapter(
    story_id: int,
    chapter_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        chapter = _author_chapter_or_404(db, story.id, chapter_id)
        return ChapterDetailOut(**_chapter_out(chapter, include_content=True))
    finally:
        db.close()


@router.post("/stories/{story_id}/chapters", response_model=ChapterDetailOut)
async def create_chapter(
    story_id: int,
    title: str | None = Form(None),
    content: str | None = Form(None),
    status: str = Form("draft"),
    file: UploadFile | None = File(None),
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    """Create a single chapter, either from a title+content pair or from an
    uploaded .txt/.docx file. This is what "Create chapters one at a time"
    and single-file upload both use."""
    if status not in {"draft", "published"}:
        raise HTTPException(status_code=400, detail="Status must be 'draft' or 'published'.")

    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        position = _next_chapter_position(db, story.id)

        if file is not None and file.filename:
            original_name = _clean_chapter_filename(file.filename)
            chapter_number, parsed_title = _parse_chapter_upload_filename(original_name)
            _reject_existing_chapter_number_conflicts(
                db,
                story.id,
                [
                    PreparedChapterUpload(
                        upload_index=0,
                        upload=file,
                        filename=original_name,
                        chapter_number=chapter_number,
                        parsed_title=parsed_title,
                    )
                ],
            )
            chapter = await _build_chapter_from_file(
                db,
                story,
                profile,
                file,
                position=position,
                title_override=title,
                original_name_override=original_name,
                parsed_title=parsed_title,
            )
        else:
            safe_title = _safe_text(title or "")
            if not safe_title:
                raise HTTPException(status_code=400, detail="Chapter title is required.")
            safe_content = _safe_long_text(content, 500_000) or ""
            if not safe_content.strip():
                raise HTTPException(status_code=400, detail="Chapter content is required.")
            chapter = Chapter(
                story_id=story.id,
                author_id=profile.id,
                title=safe_title,
                content=safe_content,
                position=position,
                status="draft",
                word_count=word_count(safe_content),
            )
            db.add(chapter)
            db.flush()

        if status == "published":
            chapter.status = "published"
            chapter.published_at = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        chapter.updated_at = now
        story.updated_at = now
        db.add(chapter)
        db.add(story)
        db.commit()
        db.refresh(chapter)
        return ChapterDetailOut(**_chapter_out(chapter, include_content=True))
    finally:
        db.close()


@router.post("/stories/{story_id}/chapters/bulk", response_model=ChapterBulkUploadResponse)
async def bulk_upload_chapters(
    story_id: int,
    files: list[UploadFile] = File(...),
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    """Bulk upload: each selected file becomes its own chapter, ordered by filename.
    the files were selected. If one file fails, the rest still get
    uploaded — every file is handled independently and committed on its
    own, so a single bad file can never roll back the good ones."""
    if not files:
        raise HTTPException(status_code=400, detail="Select at least one file to upload.")
    if len(files) > 100:
        raise HTTPException(status_code=400, detail="Upload at most 100 files at once.")

    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        prepared_uploads = _prepare_chapter_uploads(files)
        _reject_existing_chapter_number_conflicts(db, story.id, prepared_uploads)

        next_position = _next_chapter_position(db, story.id)
        results: list[dict] = []
        uploaded = 0
        failed = 0

        for item in prepared_uploads:
            display_name = item.filename
            try:
                chapter = await _build_chapter_from_file(
                    db,
                    story,
                    profile,
                    item.upload,
                    position=next_position,
                    original_name_override=item.filename,
                    parsed_title=item.parsed_title,
                )
                story.updated_at = datetime.now(timezone.utc)
                db.add(story)
                db.commit()
                db.refresh(chapter)
                next_position += 1
                uploaded += 1
                results.append(
                    {
                        "filename": display_name,
                        "status": "success",
                        "chapter_id": chapter.id,
                        "title": chapter.title,
                        "word_count": chapter.word_count,
                        "error": None,
                    }
                )
            except HTTPException as exc:
                db.rollback()
                failed += 1
                results.append(
                    {
                        "filename": display_name,
                        "status": "error",
                        "chapter_id": None,
                        "title": None,
                        "word_count": None,
                        "error": exc.detail,
                    }
                )
            except Exception:
                db.rollback()
                failed += 1
                results.append(
                    {
                        "filename": display_name,
                        "status": "error",
                        "chapter_id": None,
                        "title": None,
                        "word_count": None,
                        "error": "Unexpected error while processing this file.",
                    }
                )

        return ChapterBulkUploadResponse(
            story_id=story.id,
            uploaded=uploaded,
            failed=failed,
            results=[ChapterBulkUploadItem(**item) for item in results],
        )
    finally:
        db.close()


@router.put("/stories/{story_id}/chapters/reorder", response_model=ChapterListResponse)
def reorder_chapters(
    story_id: int,
    payload: ChapterReorderPayload,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        chapters = db.query(Chapter).filter(Chapter.story_id == story.id).all()
        chapters_by_id = {chapter.id: chapter for chapter in chapters}

        if len(payload.chapter_ids) != len(set(payload.chapter_ids)):
            raise HTTPException(status_code=400, detail="Duplicate chapter id in reorder list.")
        if set(payload.chapter_ids) != set(chapters_by_id.keys()):
            raise HTTPException(
                status_code=400,
                detail="The reorder list must include every chapter in this story exactly once.",
            )

        now = datetime.now(timezone.utc)
        for index, chapter_id in enumerate(payload.chapter_ids, start=1):
            chapter = chapters_by_id[chapter_id]
            chapter.position = index
            chapter.updated_at = now
            db.add(chapter)

        story.updated_at = now
        db.add(story)
        db.commit()

        ordered = sorted(chapters_by_id.values(), key=lambda chapter: chapter.position)
        return ChapterListResponse(
            story_id=story.id,
            chapters=[ChapterOut(**_chapter_out(chapter)) for chapter in ordered],
            total_chapters=len(ordered),
            published_chapters=sum(1 for chapter in ordered if chapter.status == "published"),
            total_words=sum(chapter.word_count or 0 for chapter in ordered),
        )
    finally:
        db.close()


@router.put("/stories/{story_id}/chapters/{chapter_id}", response_model=ChapterDetailOut)
def update_chapter(
    story_id: int,
    chapter_id: int,
    payload: ChapterUpdatePayload,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    """Edit, rename, change content, or move a chapter between draft and
    published (i.e. "Save as Draft" vs "Publish")."""
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        chapter = _author_chapter_or_404(db, story.id, chapter_id)

        if payload.title is not None:
            safe_title = _safe_text(payload.title)
            if not safe_title:
                raise HTTPException(status_code=400, detail="Chapter title is required.")
            chapter.title = safe_title

        if payload.content is not None:
            safe_content = _safe_long_text(payload.content, 500_000) or ""
            chapter.content = safe_content
            chapter.word_count = word_count(safe_content)
            chapter.upload_error = None

        if payload.status is not None:
            if payload.status not in {"draft", "published"}:
                raise HTTPException(status_code=400, detail="Status must be 'draft' or 'published'.")
            if payload.status == "published" and chapter.status != "published":
                chapter.published_at = datetime.now(timezone.utc)
            if payload.status == "draft":
                chapter.published_at = None
            chapter.status = payload.status

        now = datetime.now(timezone.utc)
        chapter.updated_at = now
        story.updated_at = now
        db.add(chapter)
        db.add(story)
        db.commit()
        db.refresh(chapter)
        return ChapterDetailOut(**_chapter_out(chapter, include_content=True))
    finally:
        db.close()


@router.post("/stories/{story_id}/chapters/{chapter_id}/publish", response_model=ChapterOut)
def publish_chapter(
    story_id: int,
    chapter_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        chapter = _author_chapter_or_404(db, story.id, chapter_id)
        if not (chapter.content or "").strip():
            raise HTTPException(status_code=400, detail="Add some content before publishing this chapter.")
        now = datetime.now(timezone.utc)
        chapter.status = "published"
        chapter.published_at = now
        chapter.updated_at = now
        story.updated_at = now
        db.add(chapter)
        db.add(story)
        db.commit()
        db.refresh(chapter)
        return ChapterOut(**_chapter_out(chapter))
    finally:
        db.close()


@router.post("/stories/{story_id}/chapters/{chapter_id}/unpublish", response_model=ChapterOut)
def unpublish_chapter(
    story_id: int,
    chapter_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    """Move a chapter back to draft (used by the "Save as Draft" action)."""
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        chapter = _author_chapter_or_404(db, story.id, chapter_id)
        now = datetime.now(timezone.utc)
        chapter.status = "draft"
        chapter.published_at = None
        chapter.updated_at = now
        story.updated_at = now
        db.add(chapter)
        db.add(story)
        db.commit()
        db.refresh(chapter)
        return ChapterOut(**_chapter_out(chapter))
    finally:
        db.close()


@router.delete("/stories/{story_id}/chapters/{chapter_id}")
def delete_chapter(
    story_id: int,
    chapter_id: int,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        story, _draft = _author_story_or_404(db, profile.id, story_id)
        chapter = _author_chapter_or_404(db, story.id, chapter_id)
        db.delete(chapter)
        db.flush()

        # Compact positions so there are no gaps left behind.
        remaining = (
            db.query(Chapter)
            .filter(Chapter.story_id == story.id)
            .order_by(Chapter.position.asc(), Chapter.id.asc())
            .all()
        )
        for index, remaining_chapter in enumerate(remaining, start=1):
            if remaining_chapter.position != index:
                remaining_chapter.position = index
                db.add(remaining_chapter)

        story.updated_at = datetime.now(timezone.utc)
        db.add(story)
        db.commit()
        return {"status": "deleted", "chapter_id": chapter_id, "story_id": story.id}
    finally:
        db.close()


@router.post("/withdrawals")
def request_withdrawal(
    payload: WithdrawalPayload,
    authorization: str | None = Header(None),
    x_user_id: str | None = Header(None),
):
    db = SessionLocal()
    try:
        profile = _author_profile_or_401(db, authorization, x_user_id)
        amount_kobo = int(payload.amount_naira * 100)
        minimum_payout_naira = _minimum_payout_naira(db)
        if payload.amount_naira < minimum_payout_naira:
            raise HTTPException(
                status_code=400,
                detail=f"Minimum withdrawal is ₦{minimum_payout_naira:,}.",
            )
        totals = _author_totals(db, profile.id)
        if payload.amount_naira > totals["available_naira"]:
            raise HTTPException(status_code=400, detail="Withdrawal exceeds available balance.")
        if not profile.bank_name or not profile.bank_account_name or not profile.bank_account_number:
            raise HTTPException(status_code=400, detail="Add bank details before requesting withdrawal.")
        row = WithdrawalRequest(
            author_id=profile.id,
            amount_kobo=amount_kobo,
            bank_name=profile.bank_name,
            bank_account_name=profile.bank_account_name,
            bank_account_number=profile.bank_account_number,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"status": "pending", "withdrawal_id": row.id}
    finally:
        db.close()
