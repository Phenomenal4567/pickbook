"""Helpers for turning an uploaded chapter file into plain, editable text.

This is the piece that was missing before: chapter uploads (particularly
.docx files) were never actually parsed into text, so authors ended up with
"chapters" that had no editable content. Every chapter-producing code path
(single upload, bulk upload, admin publish assembly) should go through
`extract_chapter_text` / `plain_text_to_html_paragraphs` below so behaviour
stays consistent.
"""

from __future__ import annotations

import io
import re

import bleach

CHAPTER_ALLOWED_EXTENSIONS = {".txt", ".docx"}


class ChapterExtractionError(ValueError):
    """Raised when a chapter file can't be turned into usable text."""


def normalize_text(value: str) -> str:
    return re.sub(r"[ \t]+", " ", (value or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


def extract_docx_text(content: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover - dependency should always be installed
        raise ChapterExtractionError(
            "DOCX support is not available on the server right now."
        ) from exc

    try:
        document = docx.Document(io.BytesIO(content))
    except Exception as exc:
        raise ChapterExtractionError(
            "That .docx file looks corrupted or isn't a real Word document."
        ) from exc

    paragraphs = [paragraph.text for paragraph in document.paragraphs]

    # Word documents sometimes carry all their text inside tables instead of
    # (or in addition to) top-level paragraphs.
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    paragraphs.append(cell.text)

    text = "\n\n".join(part.strip() for part in paragraphs if part.strip())
    return normalize_text(text)


def extract_txt_text(content: bytes) -> str:
    return normalize_text(content.decode("utf-8", errors="replace"))


def extract_chapter_text(filename: str, extension: str, content: bytes) -> str:
    """Extract plain text from an uploaded chapter file.

    Raises `ChapterExtractionError` with a user-facing message on failure.
    """
    if extension == ".docx":
        text = extract_docx_text(content)
    elif extension == ".txt":
        text = extract_txt_text(content)
    else:
        raise ChapterExtractionError(f"Unsupported file type '{extension}'.")

    if not text:
        raise ChapterExtractionError(
            f"'{filename}' doesn't contain any readable text."
        )

    return text


def chapter_title_from_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    stem = re.sub(r"\s+", " ", stem)
    return stem.title()[:255] if stem else "Untitled Chapter"


def word_count(text: str) -> int:
    return len((text or "").split())


def plain_text_to_html_paragraphs(text: str) -> str:
    """Convert plain chapter text into sanitized `<p>` paragraphs for reading."""
    paragraphs = [
        normalize_text(part)
        for part in re.split(r"\n{2,}", text or "")
        if normalize_text(part)
    ]
    return "".join(
        f"<p>{bleach.clean(part, tags=[], strip=True)}</p>" for part in paragraphs
    )
