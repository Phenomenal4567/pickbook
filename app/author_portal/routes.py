import os
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from pydantic import BaseModel, field_validator
import bleach

from app.core.limiter import limiter

router = APIRouter(
    prefix="/author",
    tags=["Author Portal"],
)

ALLOWED_EXTENSIONS = {".txt", ".docx", ".epub"}
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB


class SubmissionResponse(BaseModel):
    status: str
    title: str
    author: str
    filename: str


def _safe_text(value: str) -> str:
    """Strip all HTML tags and limit length."""
    return bleach.clean(value, tags=[], strip=True)[:500]


@router.post("/submit", response_model=SubmissionResponse)
@limiter.limit("10/minute")
async def submit_novel(
    request: Request,
    title: str = Form(..., min_length=1, max_length=500),
    author: str = Form(..., min_length=1, max_length=200),
    consent: bool = Form(...),
    file: UploadFile = File(...),
):
    if not consent:
        raise HTTPException(status_code=400, detail="Consent required.")

    # Bug fix: a filename with no dot (or only a leading dot) would raise IndexError.
    # Also catches path-traversal attempts like "../../etc/passwd".
    original_name = os.path.basename(file.filename or "")
    if "." not in original_name:
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

    return SubmissionResponse(
        status="received",
        title=safe_title,
        author=safe_author,
        filename=original_name,
    )
