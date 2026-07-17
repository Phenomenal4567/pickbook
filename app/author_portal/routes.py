from pathlib import Path, PurePath, PureWindowsPath
from datetime import datetime, timezone
import json
import re
import secrets
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, UploadFile, File, Form, Header, HTTPException, Request, Response
from pydantic import BaseModel
import bleach
from sqlalchemy.exc import IntegrityError

from app.core.auth import create_access_token, profile_id_from_token
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
    filename: str | None = None
    extension: str | None = None
    size_bytes: int | None = None
    stored_text: bool = False
    submitted_at: str | None = None
    updated_at: str | None = None


class AuthorDashboardResponse(BaseModel):
    author_id: str
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


def _safe_text(value: str) -> str:
    """Strip all HTML tags and limit length."""
    return bleach.clean(value, tags=[], strip=True)[:500]


def _safe_long_text(value: str | None, limit: int = 200_000) -> str | None:
    if value is None:
        return None
    return bleach.clean(value, tags=[], strip=True)[:limit]


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
    file: UploadFile = File(...),
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

    original_name = PureWindowsPath(file.filename or "").name
    original_name = PurePath(original_name).name

    if not original_name or original_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    if "." not in original_name.strip("."):
        raise HTTPException(status_code=400, detail="File must have an extension.")

    extension = "." + original_name.rsplit(".", 1)[-1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{extension}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # Enforce file size limit — read only the header chunk needed for the check.
    chunk = await file.read(MAX_FILE_SIZE_BYTES + 1)
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
    if not safe_title:
        raise HTTPException(status_code=400, detail="Title is required.")
    if not safe_author:
        raise HTTPException(status_code=400, detail="Author is required.")

    now = datetime.now(timezone.utc)
    manuscript_metadata = {
        "original_filename": original_name,
        "extension": extension,
        "content_type": file.content_type,
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
        filename=original_name,
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
