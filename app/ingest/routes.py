from __future__ import annotations

import asyncio
import json
import random
import re
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import bleach
import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from requests.exceptions import RequestException
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.limiter import limiter
from app.models.book import Book

router = APIRouter(prefix="/ingest", tags=["Ingestion"])

USE_PLAYWRIGHT = settings.use_playwright
BACKFILL_STOP_REQUESTS: set[str] = set()
INGEST_STOP_REQUESTS: set[str] = set()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# =========================================================
# ADMIN
# =========================================================

def _require_admin(x_admin_token: str = Header(None)):
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")


# =========================================================
# DB
# =========================================================

@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================================================
# PLAYWRIGHT
# =========================================================

try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True

except Exception as e:
    print(f"[playwright] import failed: {e}")
    PLAYWRIGHT_AVAILABLE = False


# =========================================================
# HELPERS
# =========================================================

def fetch(url: str) -> str:
    r = requests.get(
        url,
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    return r.text


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _pick_img_src(img_tag, base_url: str | None = None) -> str | None:
    if img_tag is None:
        return None

    def clean_url(value: str) -> str | None:
        value = (value or "").strip()

        if not value or value.startswith("data:") or value.endswith(".gif"):
            return None

        if value.startswith("/_next/image"):
            parsed = urlparse(value)
            image_url = parse_qs(parsed.query).get("url", [""])[0]
            value = unquote(image_url)

        if value.startswith("//"):
            value = "https:" + value

        if value.startswith("/"):
            value = urljoin(base_url or "https://alphanovel.io", value)

        if value.startswith("http"):
            return value

        return None

    for attr in (
        "data-src",
        "data-lazy-src",
        "data-original",
        "src",
    ):
        val = clean_url(img_tag.get(attr, ""))

        if val:
            return val

    srcset = img_tag.get("srcset", "").strip()

    if srcset:
        candidates = [
            item.strip().split(" ")[0]
            for item in srcset.split(",")
            if item.strip()
        ]

        for candidate in reversed(candidates):
            val = clean_url(candidate)

            if val:
                return val

    return None


def _extract_next_data(soup: BeautifulSoup) -> dict | None:
    script = soup.find("script", id="__NEXT_DATA__")

    if not script or not script.string:
        return None

    try:
        return json.loads(script.string)
    except json.JSONDecodeError:
        return None


def _clean_chapter_html(value: str) -> str:
    return bleach.clean(
        value or "",
        tags=["p", "br", "strong", "em", "i", "b"],
        attributes={},
        strip=True,
    )


def _plain_to_paragraphs(text: str) -> str:
    paragraphs = [
        normalize_text(part)
        for part in re.split(r"\n{2,}", text or "")
        if normalize_text(part)
    ]

    return "".join(
        f"<p>{bleach.clean(part, tags=[], strip=True)}</p>"
        for part in paragraphs
    )


def _book_ingest_summary(book: Book, is_new: bool = False) -> dict:
    return {
        "id": book.id,
        "title": book.title,
        "source": book.source,
        "is_new": is_new,
        "cached_chapters": _cached_chapter_count(
            getattr(book, "chapter_content", None)
        ),
        "chapters": getattr(book, "chapters_count", None),
    }


def save_books(db, books: list[dict]) -> dict:

    existing = {
        book.download: book
        for book in db.query(Book).all()
    }

    count = 0
    touched_books: list[tuple[Book, bool]] = []

    for b in books:

        if not b.get("download"):
            continue

        if (
            b.get("chapter_content")
            and not _within_chapter_cache_limit(b["chapter_content"])
        ):
            print(
                "[chapter cache SKIP] initial chapter payload exceeds "
                "MAX_CACHED_CHAPTER_BYTES"
            )
            b["chapter_content"] = None

        existing_book = existing.get(b["download"])

        if existing_book:
            changed = False

            old_title = existing_book.title or ""
            old_author = existing_book.author or ""
            is_bad_listing_title = bool(
                re.match(
                    r"^List of .+ Novels to Read Online$",
                    old_title,
                    re.IGNORECASE,
                )
            )

            if (
                b.get("source") == "anystories"
                and is_bad_listing_title
                and b.get("title")
            ):
                existing_book.title = b["title"][:255]
                changed = True

            if (
                b.get("source") == "anystories"
                and old_author in ("", "Unknown", "Unknown Author")
                and b.get("author")
                and b["author"] not in ("Unknown", "Unknown Author")
            ):
                existing_book.author = b["author"][:255]
                changed = True

            new_genre = b.get("genre")

            if (
                new_genre
                and existing_book.genre != new_genre
            ):
                existing_book.genre = new_genre
                changed = True

            for field in (
                "cover",
                "synopsis",
                "chapter_content",
                "chapters_count",
            ):
                new_value = b.get(field)
                current_value = getattr(existing_book, field, None)

                should_replace = (
                    b.get("source") == "anystories"
                    and (
                        (
                            is_bad_listing_title
                            and field in ("cover", "synopsis")
                        )
                        or (
                            field == "cover"
                            and new_value != getattr(existing_book, field, None)
                        )
                    )
                )

                if field == "chapters_count" and new_value:
                    should_replace = should_replace or (
                        not current_value
                        or int(new_value) > int(current_value)
                    )

                if field == "chapter_content" and new_value:
                    should_replace = should_replace or (
                        _cached_chapter_count(new_value)
                        > _cached_chapter_count(current_value)
                    )

                if new_value and (
                    should_replace
                    or not current_value
                ):
                    setattr(existing_book, field, new_value)
                    changed = True

            if changed:
                db.add(existing_book)

            touched_books.append((existing_book, False))

            continue

        book = Book(
            source=b.get("source", "unknown"),
            title=(b.get("title") or "Untitled")[:255],
            author=(b.get("author") or "Unknown Author")[:255],
            genre=b.get("genre", "Unknown"),
            cover=b.get("cover"),
            synopsis=b.get("synopsis"),
            chapters_count=b.get("chapters_count"),
            chapter_content=b.get("chapter_content"),
            download=b["download"],
            language="en",
        )

        db.add(book)
        db.flush()

        existing[b["download"]] = book
        touched_books.append((book, True))

        count += 1

    db.commit()

    return {
        "count": count,
        "books": [
            _book_ingest_summary(
                book,
                is_new=is_new,
            )
            for book, is_new in touched_books
        ],
    }


def _bulk_save_books(db, batch: list[dict]) -> dict:

    result = save_books(db, batch)

    print(
        f"[bulk_save] batch={len(batch)} saved={result['count']}"
    )

    return result


# =========================================================
# ANYSTORIES
# =========================================================

AS_GENRES = [
    ("romance_68ee0df8716fc476e9e06aed", "Romance"),
    ("fantasy_68ee0e02716fc476e9e06b04", "Fantasy"),
    ("werewolf_68ee0e0b716fc476e9e06b1b", "Werewolf"),
    ("dark-romance_68ee0e95716fc476e9e06c58", "Dark Romance"),
]

AS_BASE_URL = "https://www.anystories.app"

_AS_NOVEL_PATH_RE = re.compile(
    r"^/(book|novel)/[^/]+",
    re.IGNORECASE,
)


# =========================================================
# ALPHANOVEL
# =========================================================

ALPHA_BASE_URL = "https://alphanovel.io"

ALPHA_GENRES = [
    ("romance", "Romance"),
    ("werewolf", "Werewolf"),
    ("billionaire", "Billionaire"),
    ("paranormal", "Paranormal"),
    ("fantasy", "Fantasy"),
    ("ya-teen", "YA/Teen"),
    ("lgbtq", "LGBTQ+"),
]

_ALPHA_NOVEL_PATH_RE = re.compile(
    r"^/novels/[^/]+/[^/]+",
    re.IGNORECASE,
)


# =========================================================
# WEB NOVEL / LIGHT NOVEL SOURCES
# =========================================================

LIGHTNOVELWORLD_BASE_URL = "https://lightnovelworld.org"
FREEWEBNOVEL_BASE_URL = "https://freewebnovel.io"
ROYALROAD_BASE_URL = "https://www.royalroad.com"

WEB_NOVEL_SOURCES = {
    "lightnovelworld": {
        "label": "LightNovelWorld",
        "base_url": LIGHTNOVELWORLD_BASE_URL,
        "paths": ["/genre-all/"],
        "genre": "Light Novel",
        "link_re": re.compile(r"^/novel/[^/?#]+/?$", re.IGNORECASE),
        "chapter_selectors": (
            "#chapter-content",
            ".chapter-content",
            ".chapter-body",
            ".reading-content",
            ".chapter-text",
            "article",
        ),
    },
    "freewebnovel": {
        "label": "FreeWebNovel",
        "base_url": FREEWEBNOVEL_BASE_URL,
        "paths": ["/newest"],
        "genre": "Web Novel",
        "link_re": re.compile(r"^/(novel|book|webnovel)/[^/?#]+/?$", re.IGNORECASE),
        "chapter_selectors": (
            "#chapter-content",
            "#chr-content",
            ".chapter-content",
            ".chapter-c",
            ".reading-content",
            ".chapter-body",
            "article",
        ),
    },
    "royalroad": {
        "label": "Royal Road",
        "base_url": ROYALROAD_BASE_URL,
        "paths": ["/fictions/best-rated"],
        "genre": "Progression Fantasy",
        "link_re": re.compile(r"^/fiction/\d+/[^/?#]+/?$", re.IGNORECASE),
        "chapter_selectors": (
            ".chapter-content",
            ".fiction-content",
            "article",
        ),
    },
}

WEB_NOVEL_SOURCE_NAMES = set(WEB_NOVEL_SOURCES)


def _web_novel_source_list_text() -> str:
    labels = [
        config["label"]
        for config in WEB_NOVEL_SOURCES.values()
    ]

    return ", ".join(labels[:-1]) + f", or {labels[-1]}"


# =========================================================
# PARSERS
# =========================================================

def _parse_as_soup(
    soup: BeautifulSoup,
    genre: str,
) -> list[dict]:

    books: list[dict] = []

    seen: set[str] = set()

    links = soup.select("a[href]")

    print(f"[parser] links found: {len(links)}")

    for a in links:

        href = a.get("href", "").strip()

        if not href:
            continue

        if not _AS_NOVEL_PATH_RE.match(href):
            continue

        full_url = urljoin(AS_BASE_URL, href)

        if full_url in seen:
            continue

        seen.add(full_url)

        # ==========================================
        # CARD
        # ==========================================

        card = a

        for _ in range(8):

            parent = card.parent

            if parent is None:
                break

            card = parent

        # ==========================================
        # TITLE
        # ==========================================

        title = ""

        img = a.find("img") or card.find("img")

        title_candidates = [
            a.get("title", ""),
            img.get("alt", "") if img else "",
            a.get_text(" ", strip=True),
        ]

        for candidate in title_candidates:
            candidate = normalize_text(candidate)

            if (
                2 <= len(candidate) <= 200
                and not re.match(
                    r"^List of .+ Novels to Read Online$",
                    candidate,
                    re.IGNORECASE,
                )
            ):
                title = candidate
                break

        for tag in ["h1", "h2", "h3", "h4"]:

            if title:
                break

            el = card.find(tag)

            if el:

                t = normalize_text(
                    el.get_text(" ", strip=True)
                )

                if 2 <= len(t) <= 200:
                    title = t
                    break

        if not title:
            title = normalize_text(
                a.get_text(" ", strip=True)
            )

        if len(title) < 2:
            continue

        # ==========================================
        # AUTHOR
        # ==========================================

        author = "Unknown"

        for el in card.find_all(
            ["span", "small", "p", "div"]
        ):

            txt = normalize_text(
                el.get_text(" ", strip=True)
            )

            m = re.search(
                r"^[Bb]y\s+(.+)$",
                txt,
            )

            if m:
                author = m.group(1)[:150]
                break

        # ==========================================
        # COVER
        # ==========================================

        cover = None

        if img:
            cover = _pick_img_src(img)

        # ==========================================
        # SYNOPSIS
        # ==========================================

        synopsis = None

        for p in card.find_all("p"):

            txt = normalize_text(
                p.get_text(" ", strip=True)
            )

            if len(txt) > 40:
                synopsis = txt[:500]
                break

        books.append(
            {
                "source": "anystories",
                "title": title,
                "author": author,
                "genre": genre,
                "cover": cover,
                "synopsis": synopsis,
                "download": full_url,
            }
        )

    print(f"[parser] books parsed: {len(books)}")

    return books


def _parse_alpha_soup(
    soup: BeautifulSoup,
    genre: str,
) -> list[dict]:

    books: list[dict] = []

    seen: set[str] = set()

    cards = soup.select('div[class*="NovelsCard_wrapper"]')

    if not cards:
        cards = []
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            if not _ALPHA_NOVEL_PATH_RE.match(href):
                continue

            card = a
            for _ in range(6):
                parent = card.parent
                if parent is None:
                    break
                card = parent

            cards.append(card)

    print(f"[alphanovel parser] cards found: {len(cards)}")

    for card in cards:

        novel_links = []

        for a in card.select("a[href]"):
            href = a.get("href", "").strip()

            if _ALPHA_NOVEL_PATH_RE.match(href):
                novel_links.append((a, href))

        if not novel_links:
            continue

        href = novel_links[0][1]
        full_url = ALPHA_BASE_URL + href

        if full_url in seen:
            continue

        seen.add(full_url)

        title = ""

        badge_words = {
            "exclusive",
            "recommended",
            "updated",
        }

        for a, candidate_href in novel_links:
            if candidate_href != href:
                continue

            candidate = normalize_text(
                a.get_text(" ", strip=True)
            )

            candidate_words = {
                word.strip().lower()
                for word in candidate.split()
            }

            if (
                2 <= len(candidate) <= 180
                and not candidate_words.issubset(badge_words)
                and not candidate.startswith("Author:")
            ):
                title = candidate
                break

        if not title:
            slug = href.rstrip("/").split("/")[-1]
            slug = re.sub(r"-by-.+$", "", slug)
            title = slug.replace("-", " ").title()

        text = normalize_text(
            card.get_text(" ", strip=True)
        )

        author = "Unknown"

        author_match = re.search(
            r"Author:\s*(.+?)(?:\s+Status:|\s+👁|\s+⭐|$)",
            text,
        )

        if author_match:
            author = author_match.group(1).strip()[:150] or "Unknown"

        synopsis = None

        for a, candidate_href in novel_links:
            if candidate_href != href:
                continue

            candidate = normalize_text(
                a.get_text(" ", strip=True)
            )

            if len(candidate) > 80:
                synopsis = candidate[:500]
                break

        cover = None

        img = card.find("img")

        if img:
            cover = _pick_img_src(img)

        books.append(
            {
                "source": "alphanovel",
                "title": title,
                "author": author,
                "genre": genre,
                "cover": cover,
                "synopsis": synopsis,
                "download": full_url,
            }
        )

    print(f"[alphanovel parser] books parsed: {len(books)}")

    return books


def _parse_alpha_details(
    html: str,
) -> dict:

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    data = _extract_next_data(soup)

    if not data:
        return {}

    details = (
        data.get("props", {})
        .get("pageProps", {})
        .get("bookDetails", {})
    )

    book = details.get("book", {}) or {}

    chapters = []

    for chapter in details.get("chapters", []) or []:
        content = _clean_chapter_html(
            chapter.get("content", "")
        )

        if not content:
            continue

        chapters.append(
            {
                "title": chapter.get("title") or f"Chapter {len(chapters) + 1}",
                "html": content,
            }
        )

    return {
        "cover": book.get("coverUrl"),
        "synopsis": book.get("description"),
        "chapters_count": book.get("chaptersCount") or len(chapters) or None,
        "chapter_content": json.dumps(
            chapters,
            ensure_ascii=False,
        ) if chapters else None,
    }


def _first_text(
    root,
    selectors: tuple[str, ...],
) -> str | None:

    for selector in selectors:
        el = root.select_one(selector)
        if not el:
            continue

        text = normalize_text(el.get_text(" ", strip=True))
        if text:
            return text

    return None


def _extract_author_from_text(text: str) -> str | None:

    match = re.search(
        r"(?:Author|by)\s*:?\s*([A-Za-z0-9 .,'_\-&]+)",
        text or "",
        re.IGNORECASE,
    )

    if not match:
        return None

    author = normalize_text(match.group(1))
    author = re.split(
        r"\s+(?:Genre|Status|Chapter|Updated|Rating)\b",
        author,
        maxsplit=1,
    )[0]

    return author[:150] if author else None


def _clean_web_chapter_text(
    text: str,
    source: str | None = None,
) -> str:

    text = normalize_text(text)

    blocked_markers = (
        "read this novel",
        "read latest chapters",
        "hosted on",
        "visit lightnovelworld",
        "freewebnovel",
        "royal road",
        "royalroad",
        "all rights reserved",
        "support the author",
        "advertisement",
        "please enable javascript",
        "novel chapters",
        "user reviews",
        "be the first to review",
        "if you find any errors",
        "latest chapters",
        "chapter list",
        "table of contents",
    )

    lowered = text.lower()

    if any(marker in lowered for marker in blocked_markers):
        return ""

    if source == "lightnovelworld" and any(
        marker in lowered
        for marker in (
            "novel is a popular novel covering",
            "chapters have been translated",
            "currently ranked #",
            "add to library",
            "library boost",
        )
    ):
        return ""

    if source == "freewebnovel" and any(
        marker in lowered
        for marker in (
            "freewebnovel.com",
            "copyright 2019",
            "navigation",
            "novel list",
            "bookmark",
        )
    ):
        return ""

    if source == "royalroad" and any(
        marker in lowered
        for marker in (
            "follow author",
            "support the author's work",
            "fiction breaking rules",
            "leave a review",
            "remove advertisement",
        )
    ):
        return ""

    return text


def _parse_web_chapter(
    html: str,
    chapter_number: int,
    source: str | None = None,
) -> dict | None:

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "iframe",
            "svg",
            "form",
            "nav",
            "footer",
            "aside",
        ]
    ):
        tag.decompose()

    for selector in (
        ".navbar",
        ".breadcrumb",
        ".comments",
        ".review",
        ".reviews",
        ".chapter-nav",
        ".chapter-navigation",
        ".ads",
        ".ad",
        ".advertisement",
    ):
        for tag in soup.select(selector):
            tag.decompose()

    title = _first_text(
        soup,
        (
            "h1",
            "h2",
            ".chapter-title",
            ".fic-header h1",
            ".chapter-content h1",
        ),
    ) or _meta_content(soup, "og:title", "twitter:title")

    selectors = ()
    if source and source in WEB_NOVEL_SOURCES:
        selectors = WEB_NOVEL_SOURCES[source].get("chapter_selectors", ())

    containers = []
    for selector in selectors:
        containers.extend(soup.select(selector))

    if not containers:
        containers = soup.select(
            ".chapter-content, .chapter-inner, .chapter, .text-left, "
            ".fiction-content, #chapter-content, article"
        )

    paragraphs: list[str] = []
    roots = containers or [soup]

    for root in roots:
        root_paragraphs: list[str] = []

        for br in root.find_all("br"):
            br.replace_with("\n")

        for el in root.find_all(["p", "div", "section"]):
            if el.find(["p", "div"]):
                continue

            text = _clean_web_chapter_text(
                el.get_text("\n", strip=True),
                source=source,
            )

            if len(text) < 35:
                continue

            root_paragraphs.append(text)

        if len(root_paragraphs) >= max(2, len(paragraphs)):
            paragraphs = root_paragraphs

        if len(paragraphs) >= 4:
            break

    if not paragraphs:
        return None

    html_content = _plain_to_paragraphs("\n\n".join(paragraphs))

    if len(BeautifulSoup(html_content, "html.parser").get_text()) < 200:
        return None

    return {
        "title": title or f"Chapter {chapter_number}",
        "html": html_content,
    }


def _chapter_number_from_url(href: str) -> int:

    match = re.search(
        r"(?:chapter|ch)[-/]?(\d+)",
        href or "",
        re.IGNORECASE,
    )

    return int(match.group(1)) if match else 0


def _chapter_number_from_text(text: str) -> int:
    match = re.search(
        r"\b(?:chapter\s*)?(\d{1,6})\s*[:.-]",
        text or "",
        re.IGNORECASE,
    )

    return int(match.group(1)) if match else 0


def _extract_web_chapter_links(
    html: str,
    source: str,
) -> list[tuple[int, str]]:
    config = WEB_NOVEL_SOURCES[source]
    soup = BeautifulSoup(html, "html.parser")
    chapter_links = []
    seen_chapters: set[str] = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = normalize_text(a.get_text(" ", strip=True))
        parsed_path = urlparse(href).path if href.startswith("http") else href
        combined = f"{href} {text}".lower()

        if source == "royalroad":
            is_chapter = bool(
                re.search(
                    r"/fiction/\d+/[^/]+/chapter/\d+",
                    parsed_path,
                    re.IGNORECASE,
                )
            )
        elif source == "lightnovelworld":
            is_chapter = bool(
                re.search(
                    r"/novel/[^/]+/chapter/\d+/?$",
                    parsed_path,
                    re.IGNORECASE,
                )
            )
        elif source == "freewebnovel":
            is_chapter = (
                "chapter" in combined
                and bool(
                    re.search(
                        r"^/(novel|book|webnovel)/[^/?#]+",
                        parsed_path,
                        re.IGNORECASE,
                    )
                )
            )
        else:
            is_chapter = "chapter" in combined

        if not is_chapter:
            continue

        chapter_url = urljoin(config["base_url"], href)

        if chapter_url in seen_chapters:
            continue

        seen_chapters.add(chapter_url)
        chapter_links.append(
            (
                _chapter_number_from_text(text)
                or _chapter_number_from_url(href)
                or len(chapter_links) + 1,
                chapter_url,
            )
        )

    chapter_links.sort(key=lambda item: item[0])

    return chapter_links


def _parse_webnovel_listing(
    html: str,
    source: str,
    genre: str,
) -> list[dict]:

    config = WEB_NOVEL_SOURCES[source]
    base_url = config["base_url"]
    link_re = config["link_re"]
    soup = BeautifulSoup(html, "html.parser")
    books: list[dict] = []
    seen: set[str] = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        parsed_path = urlparse(href).path if href.startswith("http") else href

        if not link_re.match(parsed_path):
            continue

        if "/chapter/" in parsed_path.lower():
            continue

        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue

        seen.add(full_url)

        card = a
        for _ in range(7):
            if card.parent is None:
                break
            card = card.parent

        img = a.find("img") or card.find("img")
        title = normalize_text(
            a.get("title")
            or (img.get("alt") if img else "")
            or a.get_text(" ", strip=True)
        )

        title = re.sub(r"^Read\s+", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+Novel$", "", title, flags=re.IGNORECASE)

        if len(title) < 2 or title.lower() in {"read", "novel", "chapter"}:
            slug = parsed_path.rstrip("/").split("/")[-1]
            title = re.sub(r"^\d+-", "", slug).replace("-", " ").title()

        card_text = normalize_text(card.get_text(" ", strip=True))
        author = _extract_author_from_text(card_text) or "Unknown"
        synopsis = None

        for el in card.find_all(["p", "div"]):
            text = normalize_text(el.get_text(" ", strip=True))
            if 70 <= len(text) <= 700 and title.lower() not in text.lower():
                synopsis = text[:500]
                break

        books.append(
            {
                "source": source,
                "title": title[:255],
                "author": author,
                "genre": genre,
                "cover": _pick_img_src(img, base_url) if img else None,
                "synopsis": synopsis,
                "download": full_url,
            }
        )

    print(f"[{source} parser] books parsed: {len(books)}")
    return books


def _parse_webnovel_details(
    html: str,
    detail_url: str,
    source: str,
    max_public_chapters: int | None = None,
) -> dict:

    config = WEB_NOVEL_SOURCES[source]
    soup = BeautifulSoup(html, "html.parser")

    title = _first_text(
        soup,
        (
            "h1",
            ".novel-title",
            ".fic-title h1",
            ".profile-info h1",
            ".book-title",
        ),
    ) or _meta_content(soup, "og:title", "twitter:title")

    author = _first_text(
        soup,
        (
            ".author a",
            ".fic-author",
            ".fiction-info a[href*='/profile/']",
            "a[href*='/author/']",
        ),
    )

    page_text = normalize_text(soup.get_text(" ", strip=True))
    author = author or _extract_author_from_text(page_text)

    cover = _meta_content(soup, "og:image", "twitter:image")
    if not cover:
        img = soup.select_one(".cover img, .novel-cover img, .fic-header img, img")
        cover = _pick_img_src(img, config["base_url"]) if img else None

    synopsis = _meta_content(soup, "description", "og:description", "twitter:description")
    if not synopsis:
        synopsis = _first_text(
            soup,
            (
                ".description",
                ".summary",
                ".synopsis",
                ".fiction-description",
                ".profile-info .margin-bottom-10",
            ),
        )

    chapter_links = _extract_web_chapter_links(
        html,
        source,
    )
    chapters_count = len(chapter_links) or None
    chapter_limit = (
        settings.initial_chapters_per_book
        if max_public_chapters is None
        else max_public_chapters
    )
    chapter_limit = min(
        settings.max_chapters_per_book,
        chapter_limit,
    )

    chapters = []

    for chapter_number, chapter_url in chapter_links[:chapter_limit]:
        try:
            chapter = _parse_web_chapter(
                fetch(chapter_url),
                chapter_number,
                source=source,
            )
        except RequestException as e:
            print(f"[{source} chapter ERROR] {chapter_url}: {e}")
            continue

        if chapter:
            chapters.append(chapter)

        time.sleep(0.2)

    return {
        "title": title,
        "author": author,
        "cover": cover,
        "synopsis": synopsis,
        "chapters_count": chapters_count,
        "chapter_content": json.dumps(chapters, ensure_ascii=False) if chapters else None,
    }


def _meta_content(
    soup: BeautifulSoup,
    *names: str,
) -> str | None:

    for name in names:
        meta = soup.find(
            "meta",
            attrs={"property": name},
        ) or soup.find(
            "meta",
            attrs={"name": name},
        )

        if meta and meta.get("content"):
            return normalize_text(
                meta.get("content", "")
            )

    return None


def _parse_int(value: str) -> int | None:

    value = (value or "").replace(",", "").strip()

    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def _parse_as_jsonld_book(
    soup: BeautifulSoup,
) -> dict:

    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        raw = script.get_text(strip=True)

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        nodes = data.get("@graph", [data]) if isinstance(data, dict) else []

        for node in nodes:
            if not isinstance(node, dict):
                continue

            node_type = node.get("@type")

            if (
                node_type != "Book"
                and not (
                    isinstance(node_type, list)
                    and "Book" in node_type
                )
            ):
                continue

            image = node.get("image")
            cover = None

            if isinstance(image, dict):
                cover = image.get("url")
            elif isinstance(image, str):
                cover = image

            author = node.get("author")
            author_name = None

            if isinstance(author, dict):
                author_name = author.get("name")
            elif isinstance(author, str):
                author_name = author

            return {
                "title": node.get("name") or node.get("headline"),
                "author": author_name,
                "cover": cover,
                "synopsis": node.get("description"),
            }

    return {}


def _parse_as_chapter(
    html: str,
    chapter_number: int,
) -> dict | None:

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    title = None

    for heading in soup.find_all(["h1", "h2"]):
        heading_text = normalize_text(
            heading.get_text(" ", strip=True)
        )

        if heading_text:
            title = heading_text[:180]
            break

    if not title:
        title = _meta_content(
            soup,
            "og:title",
            "twitter:title",
        )

    paragraphs = []

    for p in soup.find_all("p"):
        text = normalize_text(
            p.get_text(" ", strip=True)
        )

        if len(text) < 35:
            continue

        lowered = text.lower()

        if any(
            marker in lowered
            for marker in (
                "download the app",
                "read romance",
                "explore 50,000",
                "all rights reserved",
            )
        ):
            continue

        paragraphs.append(text)

    if not paragraphs:
        return None

    html_content = _plain_to_paragraphs(
        "\n\n".join(paragraphs)
    )

    if len(BeautifulSoup(html_content, "html.parser").get_text()) < 120:
        return None

    return {
        "title": title or f"Chapter {chapter_number}",
        "html": html_content,
    }


def _parse_as_details(
    html: str,
    detail_url: str,
    max_public_chapters: int | None = None,
) -> dict:

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    details = _parse_as_jsonld_book(soup)

    cover = details.get("cover") or _meta_content(
        soup,
        "og:image",
        "twitter:image",
    )

    synopsis = details.get("synopsis") or _meta_content(
        soup,
        "description",
        "og:description",
        "twitter:description",
    )

    page_text = normalize_text(
        soup.get_text(" ", strip=True)
    )

    chapters_count = None
    chapters_match = re.search(
        r"(\d[\d,]*)\s+Chapters\b",
        page_text,
        re.IGNORECASE,
    )

    if chapters_match:
        chapters_count = _parse_int(
            chapters_match.group(1)
        )

    chapter_limit = settings.max_chapters_per_book

    if max_public_chapters is not None:
        chapter_limit = min(
            chapter_limit,
            max_public_chapters,
        )

    if chapters_count:
        chapter_limit = min(
            chapter_limit,
            chapters_count,
        )

    chapters = []
    misses = 0

    for chapter_number in range(1, chapter_limit + 1):
        try:
            chapter_html = fetch(
                f"{detail_url.rstrip('/')}/chapters/{chapter_number}"
            )
        except RequestException as e:
            print(
                f"[anystories chapter ERROR] "
                f"{detail_url} chapter {chapter_number}: {e}"
            )
            misses += 1
            if not chapters_count and misses >= 3:
                break
            continue

        chapter = _parse_as_chapter(
            chapter_html,
            chapter_number,
        )

        if chapter:
            chapters.append(chapter)
            misses = 0
        else:
            misses += 1
            if not chapters_count and misses >= 3:
                break

        time.sleep(0.2)

    return {
        "title": details.get("title"),
        "author": details.get("author"),
        "cover": cover,
        "synopsis": synopsis,
        "chapters_count": chapters_count or len(chapters) or None,
        "chapter_content": json.dumps(
            chapters,
            ensure_ascii=False,
        ) if chapters else None,
    }


def _enrich_as_books_with_details(
    books: list[dict],
    max_public_chapters: int | None = None,
) -> list[dict]:

    for book in books:
        try:
            detail_html = fetch(book["download"])
            details = _parse_as_details(
                detail_html,
                book["download"],
                max_public_chapters=max_public_chapters,
            )

            for key, value in details.items():
                if value:
                    book[key] = value

        except RequestException as e:
            print(
                f"[anystories detail ERROR] "
                f"{book.get('title')}: {e}"
            )

        time.sleep(0.3)

    return books


def _source_chapter_url(
    book: Book,
    chapter_number: int,
) -> str:

    source_url = book.download or ""

    if (
        book.source == "anystories"
        and source_url
    ):
        return f"{source_url.rstrip('/')}/chapters/{chapter_number}"

    return source_url


def _cached_chapter_count(
    chapter_content: str | None,
) -> int:

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


def _encode_cached_chapters(
    chapters: list[dict],
) -> str:

    return json.dumps(
        chapters,
        ensure_ascii=False,
    )


def _within_chapter_cache_limit(
    chapter_content: str,
) -> bool:

    limit = settings.max_cached_chapter_bytes

    return limit <= 0 or len(chapter_content.encode("utf-8")) <= limit


def _load_chapters_from_book(
    book: Book,
) -> list[dict]:

    if not book.chapter_content:
        return []

    try:
        chapters = json.loads(book.chapter_content)
    except json.JSONDecodeError:
        return []

    return chapters if isinstance(chapters, list) else []


def _store_chapter(
    db,
    book: Book,
    chapter_number: int,
    chapter: dict,
) -> bool:

    chapters = _load_chapters_from_book(book)
    index = chapter_number - 1

    while len(chapters) <= index:
        missing_number = len(chapters) + 1
        chapters.append(
            {
                "title": f"Chapter {missing_number}",
                "html": "",
            }
        )

    chapters[index] = {
        "title": chapter.get("title") or f"Chapter {chapter_number}",
        "html": _clean_chapter_html(chapter.get("html") or ""),
    }

    chapter_content = _encode_cached_chapters(chapters)

    if not _within_chapter_cache_limit(chapter_content):
        print(
            f"[chapter cache SKIP] book_id={book.id} "
            f"chapter={chapter_number} exceeds MAX_CACHED_CHAPTER_BYTES"
        )
        return False

    book.chapter_content = chapter_content

    if (
        not book.chapters_count
        or book.chapters_count < chapter_number
    ):
        book.chapters_count = chapter_number

    db.add(book)
    db.commit()
    return True


def _fetch_chapter_from_source(
    book: Book,
    chapter_number: int,
) -> dict | None:

    source = (book.source or "").lower()

    if source == "anystories" and book.download:
        try:
            html = fetch(
                _source_chapter_url(
                    book,
                    chapter_number,
                )
            )
        except RequestException as e:
            print(
                f"[chapter fetch ERROR] "
                f"{book.download} chapter {chapter_number}: {e}"
            )
            return None

        return _parse_as_chapter(
            html,
            chapter_number,
        )

    if source == "alphanovel" and book.download:
        try:
            details = _parse_alpha_details(
                fetch(book.download)
            )
        except RequestException as e:
            print(
                f"[chapter fetch ERROR] "
                f"{book.download}: {e}"
            )
            return None

        raw_chapters = details.get("chapter_content")

        if not raw_chapters:
            return None

        try:
            chapters = json.loads(raw_chapters)
        except json.JSONDecodeError:
            return None

        index = chapter_number - 1

        if 0 <= index < len(chapters):
            return chapters[index]

    if source in WEB_NOVEL_SOURCES and book.download:
        try:
            detail_html = fetch(book.download)
        except RequestException as e:
            print(
                f"[chapter fetch ERROR] "
                f"{book.download}: {e}"
            )
            return None

        chapter_links = _extract_web_chapter_links(
            detail_html,
            source,
        )
        index = chapter_number - 1

        if 0 <= index < len(chapter_links):
            try:
                return _parse_web_chapter(
                    fetch(chapter_links[index][1]),
                    chapter_number,
                    source=source,
                )
            except RequestException as e:
                print(
                    f"[chapter fetch ERROR] "
                    f"{chapter_links[index][1]}: {e}"
                )
                return None

    return None


def _cache_book_chapters(
    db,
    book: Book,
    max_chapters: int | None = None,
    force: bool = False,
    stop_key: str | None = None,
) -> dict:

    target = max_chapters or settings.max_chapters_per_book

    if book.chapters_count:
        target = min(
            target,
            book.chapters_count,
        )

    cached_before = _cached_chapter_count(
        book.chapter_content
    )
    fetched = 0
    unavailable = 0
    stopped = False
    note = None

    if (book.source or "").lower() == "alphanovel" and book.download:
        try:
            details = _parse_alpha_details(
                fetch(book.download)
            )
        except RequestException as e:
            print(
                f"[chapter backfill ERROR] "
                f"{book.download}: {e}"
            )
            details = {}

        raw_chapters = details.get("chapter_content")

        if raw_chapters:
            try:
                alpha_chapters = json.loads(raw_chapters)
            except json.JSONDecodeError:
                alpha_chapters = []

            if isinstance(alpha_chapters, list):
                exposed = len(alpha_chapters)

                for chapter_number, chapter in enumerate(
                    alpha_chapters[:target],
                    start=1,
                ):
                    if stop_key and stop_key in BACKFILL_STOP_REQUESTS:
                        stopped = True
                        break

                    chapters = _load_chapters_from_book(book)
                    index = chapter_number - 1

                    if (
                        not force
                        and 0 <= index < len(chapters)
                        and isinstance(chapters[index], dict)
                        and chapters[index].get("html")
                    ):
                        continue

                    if not isinstance(chapter, dict) or not chapter.get("html"):
                        unavailable += 1
                        continue

                    stored = _store_chapter(
                        db,
                        book,
                        chapter_number,
                        chapter,
                    )
                    if not stored:
                        note = (
                            "Chapter cache stopped because this book reached "
                            "the configured storage limit."
                        )
                        break
                    db.refresh(book)
                    fetched += 1

                if exposed < target:
                    note = (
                        f"Alphanovel page exposed {exposed} chapter(s); "
                        "no additional chapter HTML was present in the page data."
                    )

                return {
                    "book_id": book.id,
                    "title": book.title,
                    "chapters_targeted": target,
                    "cached_before": cached_before,
                    "cached_after": _cached_chapter_count(
                        book.chapter_content
                    ),
                    "fetched": fetched,
                    "unavailable": unavailable,
                    "stopped": stopped,
                    "note": note,
                }
        else:
            return {
                "book_id": book.id,
                "title": book.title,
                "chapters_targeted": target,
                "cached_before": cached_before,
                "cached_after": _cached_chapter_count(
                    book.chapter_content
                ),
                "fetched": fetched,
                "unavailable": target,
                "stopped": stopped,
                "note": (
                    "Alphanovel did not expose chapter HTML in the fetched page data."
                ),
            }

    for chapter_number in range(1, target + 1):
        if stop_key and stop_key in BACKFILL_STOP_REQUESTS:
            stopped = True
            break

        chapters = _load_chapters_from_book(book)
        index = chapter_number - 1

        if (
            not force
            and 0 <= index < len(chapters)
            and isinstance(chapters[index], dict)
            and chapters[index].get("html")
        ):
            continue

        chapter = _fetch_chapter_from_source(
            book,
            chapter_number,
        )

        if not chapter or not chapter.get("html"):
            unavailable += 1
            continue

        stored = _store_chapter(
            db,
            book,
            chapter_number,
            chapter,
        )
        if not stored:
            note = (
                "Chapter cache stopped because this book reached "
                "the configured storage limit."
            )
            break
        db.refresh(book)
        fetched += 1
        time.sleep(0.2)

    return {
        "book_id": book.id,
        "title": book.title,
        "chapters_targeted": target,
        "cached_before": cached_before,
        "cached_after": _cached_chapter_count(
            book.chapter_content
        ),
        "fetched": fetched,
        "unavailable": unavailable,
        "stopped": stopped,
        "note": note,
    }


# =========================================================
# PLAYWRIGHT FETCH
# =========================================================

async def _async_fetch_as(
    slug: str,
    page_number: int = 1,
) -> str:

    url = (
        f"https://www.anystories.app/genre/"
        f"{slug}?order=score&page={page_number}"
    )

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={
                "width": 1440,
                "height": 2400,
            },
            locale="en-US",
        )

        page = await context.new_page()

        print(f"[playwright] opening: {url}")

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=120000,
        )

        await page.wait_for_timeout(5000)

        # ==========================================
        # SCROLL
        # ==========================================

        previous_height = 0

        for _ in range(30):

            current_height = await page.evaluate(
                "document.body.scrollHeight"
            )

            if current_height == previous_height:
                break

            previous_height = current_height

            await page.mouse.wheel(0, 10000)

            await page.wait_for_timeout(2500)

        # ==========================================
        # FORCE IMAGES LOAD
        # ==========================================

        try:

            imgs = await page.locator("img").all()

            print(
                f"[playwright] images found: {len(imgs)}"
            )

            for img in imgs[:120]:

                try:
                    await img.scroll_into_view_if_needed(
                        timeout=2000
                    )

                    await page.wait_for_timeout(100)

                except Exception:
                    pass

        except Exception as e:
            print(f"[playwright] image error: {e}")

        await page.wait_for_timeout(3000)

        title = await page.title()

        print(f"[playwright] title: {title}")

        html = await page.content()

        print(
            f"[playwright] html size: {len(html)}"
        )

        await browser.close()

        return html


def scrape_as_playwright(
    slug: str,
    genre: str,
    page_number: int = 1,
    max_public_chapters: int | None = None,
    max_books: int | None = None,
    stop_key: str | None = None,
) -> list[dict]:

    if not PLAYWRIGHT_AVAILABLE:

        print(
            "[anystories] Playwright unavailable"
        )

        return scrape_as_static(slug, genre)

    try:

        html = asyncio.run(
            _async_fetch_as(
                slug,
                page_number=page_number,
            )
        )

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        books = _parse_as_soup(
            soup,
            genre,
        )

        if not books:
            print(
                f"[anystories] no books parsed "
                f"for {slug}"
            )

        return _enrich_as_books_with_details(
            books,
            max_public_chapters=max_public_chapters,
        )

    except Exception as e:

        print(
            f"[anystories ERROR] {slug}: {e}; "
            "falling back to static fetch"
        )

        try:
            return scrape_as_static(slug, genre)
        except RequestException as static_error:
            print(
                f"[anystories static ERROR] {slug}: "
                f"{static_error}"
            )
            return []


def scrape_as_static(
    slug: str,
    genre: str,
    page_number: int = 1,
    max_public_chapters: int | None = None,
    max_books: int | None = None,
    stop_key: str | None = None,
) -> list[dict]:

    url = (
        f"https://www.anystories.app/genre/"
        f"{slug}?order=score&page={page_number}"
    )

    html = fetch(url)

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    books = _parse_as_soup(
        soup,
        genre,
    )

    return _enrich_as_books_with_details(
        books,
        max_public_chapters=max_public_chapters,
    )


def scrape_alpha_static(
    slug: str,
    genre: str,
    page_number: int = 1,
    max_public_chapters: int | None = None,
    max_books: int | None = None,
    stop_key: str | None = None,
) -> list[dict]:

    url = f"{ALPHA_BASE_URL}/novels/{slug}"

    html = fetch(url)

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    books = _parse_alpha_soup(
        soup,
        genre,
    )

    for book in books:
        try:
            detail_html = fetch(book["download"])
            details = _parse_alpha_details(detail_html)

            for key, value in details.items():
                if value:
                    book[key] = value

        except RequestException as e:
            print(
                f"[alphanovel detail ERROR] "
                f"{book.get('title')}: {e}"
            )

        time.sleep(0.3)

    return books


def scrape_webnovel_static(
    slug: str,
    genre: str,
    page_number: int = 1,
    max_public_chapters: int | None = None,
    max_books: int | None = None,
    stop_key: str | None = None,
) -> list[dict]:

    config = WEB_NOVEL_SOURCES[slug]
    path = config["paths"][0]
    separator = "&" if "?" in path else "?"
    url = f"{config['base_url']}{path}{separator}page={page_number}"

    html = fetch(url)
    books = _parse_webnovel_listing(html, slug, genre)

    if max_books is not None:
        books = books[:max_books]

    for book in books:
        if stop_key and stop_key in INGEST_STOP_REQUESTS:
            break

        try:
            details = _parse_webnovel_details(
                fetch(book["download"]),
                book["download"],
                slug,
                max_public_chapters=max_public_chapters,
            )

            for key, value in details.items():
                if value:
                    book[key] = value

        except RequestException as e:
            print(
                f"[{slug} detail ERROR] "
                f"{book.get('title')}: {e}"
            )

        time.sleep(0.3)

    return books


# =========================================================
# INGEST CORE
# =========================================================

def _run_ingest_pipeline(
    source_name: str,
    genres: list[tuple[str, str]],
    scrape_fn,
    batch_size: int = 50,
    pages_per_genre: int = 1,
    max_public_chapters: int | None = None,
    max_books_per_page: int | None = None,
    stop_key: str | None = None,
) -> dict:

    total = 0
    stopped = False
    saved_books: list[dict] = []

    batch_buffer: list[dict] = []

    with get_db() as db:

        for slug, genre in genres:
            for page_number in range(1, pages_per_genre + 1):
                if stop_key and stop_key in INGEST_STOP_REQUESTS:
                    stopped = True
                    break

                try:

                    books = scrape_fn(
                        slug,
                        genre,
                        page_number=page_number,
                        max_public_chapters=max_public_chapters,
                        max_books=max_books_per_page,
                        stop_key=stop_key,
                    )

                    batch_buffer.extend(books)

                    if stop_key and stop_key in INGEST_STOP_REQUESTS:
                        stopped = True

                    print(
                        f"[{source_name}] "
                        f"{genre} page {page_number}: "
                        f"{len(books)} scraped"
                    )

                    if (
                        len(batch_buffer)
                        >= batch_size
                    ):

                        save_result = _bulk_save_books(
                            db,
                            batch_buffer,
                        )

                        total += save_result["count"]
                        saved_books.extend(save_result["books"])

                        batch_buffer = []

                except Exception as e:

                    print(
                        f"[{source_name} ERROR] "
                        f"{genre} page {page_number}: {e}"
                    )

                delay = random.randint(1, 3)

                time.sleep(delay)

                if stopped:
                    break

            if stopped:
                break

        if batch_buffer:

            save_result = _bulk_save_books(
                db,
                batch_buffer,
            )

            total += save_result["count"]
            saved_books.extend(save_result["books"])

    if stopped and stop_key:
        INGEST_STOP_REQUESTS.discard(stop_key)

    return {
        "status": "stopped" if stopped else "success",
        "source": source_name,
        "count": total,
        "books": saved_books,
        "stopped": stopped,
        "mode": (
            "playwright"
            if USE_PLAYWRIGHT
            else "static"
        ),
        "pages_per_genre": pages_per_genre,
        "chapters_per_book": max_public_chapters,
        "max_books_per_page": max_books_per_page,
    }


# =========================================================
# INGEST HELPERS
# =========================================================

def _run_anystories_ingest(
    pages_per_genre: int | None = None,
    max_public_chapters: int | None = None,
    stop_key: str | None = None,
):

    if USE_PLAYWRIGHT and PLAYWRIGHT_AVAILABLE:
        scrape_fn = scrape_as_playwright
    else:
        scrape_fn = scrape_as_static

    return _run_ingest_pipeline(
        "anystories",
        AS_GENRES,
        scrape_fn,
        pages_per_genre=pages_per_genre or settings.anystories_pages_per_genre,
        max_public_chapters=(
            settings.initial_chapters_per_book
            if max_public_chapters is None
            else max_public_chapters
        ),
        stop_key=stop_key,
    )


def _run_alphanovel_ingest(
    stop_key: str | None = None,
):

    return _run_ingest_pipeline(
        "alphanovel",
        ALPHA_GENRES,
        scrape_alpha_static,
        stop_key=stop_key,
    )


def _run_webnovel_ingest(
    source: str,
    pages_per_source: int = 1,
    max_public_chapters: int | None = None,
    max_books_per_page: int | None = None,
    stop_key: str | None = None,
):

    source = source.lower().strip()

    if source not in WEB_NOVEL_SOURCES:
        raise HTTPException(status_code=400, detail="Unknown web novel source.")

    config = WEB_NOVEL_SOURCES[source]

    return _run_ingest_pipeline(
        source,
        [(source, config["genre"])],
        scrape_webnovel_static,
        pages_per_genre=pages_per_source,
        max_public_chapters=(
            settings.initial_chapters_per_book
            if max_public_chapters is None
            else max_public_chapters
        ),
        max_books_per_page=max_books_per_page,
        stop_key=stop_key,
    )


def _run_all_webnovel_ingests(
    pages_per_source: int = 1,
    max_public_chapters: int | None = None,
    max_books_per_page: int | None = None,
    stop_key: str | None = None,
) -> dict:
    results = {
        source: _run_webnovel_ingest(
            source,
            pages_per_source=pages_per_source,
            max_public_chapters=max_public_chapters,
            max_books_per_page=max_books_per_page,
            stop_key=stop_key,
        )
        for source in WEB_NOVEL_SOURCES
        if not stop_key or stop_key not in INGEST_STOP_REQUESTS
    }
    stopped = (
        bool(stop_key and stop_key in INGEST_STOP_REQUESTS)
        or any(item.get("stopped", False) for item in results.values())
    )

    if stopped and stop_key:
        INGEST_STOP_REQUESTS.discard(stop_key)

    return {
        "status": "stopped" if stopped else "success",
        "source": "webnovels",
        "count": sum(item["count"] for item in results.values()),
        "books": [
            book
            for result in results.values()
            for book in result.get("books", [])
        ],
        "stopped": stopped,
        "sources": results,
        "pages_per_source": pages_per_source,
        "chapters_per_book": (
            settings.initial_chapters_per_book
            if max_public_chapters is None
            else max_public_chapters
        ),
        "max_books_per_page": max_books_per_page,
    }


# =========================================================
# ROUTES
# =========================================================

@router.get(
    "/anystories",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("5/minute")
def ingest_anystories(
    request: Request,
    pages_per_genre: int | None = Query(None, ge=1, le=20),
    chapters_per_book: int | None = Query(None, ge=0, le=100),
):

    return _run_anystories_ingest(
        pages_per_genre=pages_per_genre,
        max_public_chapters=chapters_per_book,
        stop_key="anystories:ingest",
    )


@router.get(
    "/webnovels",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("3/minute")
def ingest_webnovels(
    request: Request,
    pages: int = Query(1, ge=1, le=10),
    chapters_per_book: int | None = Query(None, ge=0, le=100),
    max_books_per_page: int | None = Query(10, ge=1, le=100),
):

    return _run_all_webnovel_ingests(
        pages_per_source=pages,
        max_public_chapters=chapters_per_book,
        max_books_per_page=max_books_per_page,
        stop_key="webnovels:ingest",
    )


@router.post(
    "/webnovels/stop",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("30/hour")
def stop_webnovel_ingest(
    request: Request,
):

    INGEST_STOP_REQUESTS.add("webnovels:ingest")

    for source in WEB_NOVEL_SOURCES:
        INGEST_STOP_REQUESTS.add(f"{source}:ingest")

    return {
        "status": "stop_requested",
        "source": "webnovels",
        "sources": list(WEB_NOVEL_SOURCES),
    }


@router.post(
    "/{source}/stop",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("30/hour")
def stop_source_ingest(
    source: str,
    request: Request,
):

    source = source.lower().strip()
    allowed_sources = {"anystories", "alphanovel", *WEB_NOVEL_SOURCE_NAMES}

    if source not in allowed_sources:
        raise HTTPException(
            status_code=400,
            detail=(
                "Source must be anystories, alphanovel, "
                f"{_web_novel_source_list_text()}."
            ),
        )

    INGEST_STOP_REQUESTS.add(f"{source}:ingest")

    return {
        "status": "stop_requested",
        "source": source,
    }


@router.post(
    "/webnovels/full-chapters",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("6/hour")
def backfill_webnovel_full_chapters(
    request: Request,
    limit: int = Query(2, ge=1, le=50),
    max_chapters: int | None = Query(25, ge=1, le=100),
    force: bool = Query(False),
):

    results = {}
    stopped = False

    for source in WEB_NOVEL_SOURCES:
        result = backfill_source_full_chapters(
            source=source,
            request=request,
            limit=limit,
            max_chapters=max_chapters,
            force=force,
        )
        results[source] = result
        stopped = stopped or result.get("stopped", False)

        if stopped:
            break

    return {
        "status": "stopped" if stopped else "success",
        "source": "webnovels",
        "books_checked": sum(
            item.get("books_checked", 0)
            for item in results.values()
        ),
        "chapters_fetched": sum(
            item.get("chapters_fetched", 0)
            for item in results.values()
        ),
        "stopped": stopped,
        "sources": results,
    }


@router.post(
    "/{source}/full-chapters",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("12/hour")
def backfill_source_full_chapters(
    source: str,
    request: Request,
    limit: int = Query(5, ge=1, le=50),
    max_chapters: int | None = Query(25, ge=1, le=100),
    force: bool = Query(False),
    book_id: int | None = Query(None, ge=1),
    title: str | None = Query(None),
):

    source = source.lower().strip()

    allowed_sources = {"anystories", "alphanovel", *WEB_NOVEL_SOURCE_NAMES}

    if source not in allowed_sources:
        raise HTTPException(
            status_code=400,
            detail=(
                "Source must be anystories, alphanovel, "
                f"{_web_novel_source_list_text()}."
            ),
        )

    stop_key = f"{source}:full-chapters"
    BACKFILL_STOP_REQUESTS.discard(stop_key)
    results = []
    stopped = False

    with get_db() as db:
        query = db.query(Book).filter(
            Book.source == source,
            Book.download.isnot(None),
        )

        if book_id:
            books = query.filter(Book.id == book_id).limit(1).all()
        elif title:
            books = (
                query.filter(Book.title.ilike(f"%{title.strip()}%"))
                .order_by(Book.id.desc())
                .limit(limit)
                .all()
            )
        else:
            random_order = (
                func.rand()
                if db.bind and db.bind.dialect.name == "mysql"
                else func.random()
            )
            candidates = (
                query.order_by(random_order)
                .limit(max(limit * 8, 40))
                .all()
            )
            books = []

            for candidate in candidates:
                candidate_target = max_chapters or settings.max_chapters_per_book
                if candidate.chapters_count:
                    candidate_target = min(candidate_target, candidate.chapters_count)

                if force or _cached_chapter_count(candidate.chapter_content) < candidate_target:
                    books.append(candidate)

                if len(books) >= limit:
                    break

        for book in books:
            if stop_key in BACKFILL_STOP_REQUESTS:
                stopped = True
                break

            if not book.download:
                continue

            result = _cache_book_chapters(
                db,
                book,
                max_chapters=max_chapters,
                force=force,
                stop_key=stop_key,
            )
            results.append(result)

            if result.get("stopped"):
                stopped = True
                break

    if stopped:
        BACKFILL_STOP_REQUESTS.discard(stop_key)

    return {
        "status": "stopped" if stopped else "success",
        "source": source,
        "books_checked": len(results),
        "chapters_fetched": sum(
            item["fetched"]
            for item in results
        ),
        "stopped": stopped,
        "results": results,
    }


@router.post(
    "/webnovels/full-chapters/stop",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("30/hour")
def stop_webnovel_full_chapters(
    request: Request,
):

    for source in WEB_NOVEL_SOURCES:
        BACKFILL_STOP_REQUESTS.add(f"{source}:full-chapters")

    return {
        "status": "stop_requested",
        "source": "webnovels",
        "sources": list(WEB_NOVEL_SOURCES),
    }


@router.post(
    "/{source}/full-chapters/stop",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("30/hour")
def stop_source_full_chapters(
    source: str,
    request: Request,
):

    source = source.lower().strip()

    allowed_sources = {"anystories", "alphanovel", *WEB_NOVEL_SOURCE_NAMES}

    if source not in allowed_sources:
        raise HTTPException(
            status_code=400,
            detail=(
                "Source must be anystories, alphanovel, "
                f"{_web_novel_source_list_text()}."
            ),
        )

    BACKFILL_STOP_REQUESTS.add(f"{source}:full-chapters")

    return {
        "status": "stop_requested",
        "source": source,
    }


@router.post(
    "/anystories/full-chapters",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("12/hour")
def backfill_anystories_full_chapters(
    request: Request,
    limit: int = Query(5, ge=1, le=50),
    max_chapters: int | None = Query(25, ge=1, le=100),
    force: bool = Query(False),
    book_id: int | None = Query(None, ge=1),
    title: str | None = Query(None),
):

    return backfill_source_full_chapters(
        source="anystories",
        request=request,
        limit=limit,
        max_chapters=max_chapters,
        force=force,
        book_id=book_id,
        title=title,
    )


@router.get(
    "/alphanovel",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("5/minute")
def ingest_alphanovel(request: Request):

    return _run_alphanovel_ingest(
        stop_key="alphanovel:ingest",
    )


@router.get(
    "/lightnovelworld",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("5/minute")
def ingest_lightnovelworld(
    request: Request,
    pages: int = Query(1, ge=1, le=10),
    chapters_per_book: int | None = Query(None, ge=0, le=100),
    max_books_per_page: int | None = Query(10, ge=1, le=100),
):

    return _run_webnovel_ingest(
        "lightnovelworld",
        pages_per_source=pages,
        max_public_chapters=chapters_per_book,
        max_books_per_page=max_books_per_page,
        stop_key="lightnovelworld:ingest",
    )


@router.get(
    "/freewebnovel",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("5/minute")
def ingest_freewebnovel(
    request: Request,
    pages: int = Query(1, ge=1, le=10),
    chapters_per_book: int | None = Query(None, ge=0, le=100),
    max_books_per_page: int | None = Query(10, ge=1, le=100),
):

    return _run_webnovel_ingest(
        "freewebnovel",
        pages_per_source=pages,
        max_public_chapters=chapters_per_book,
        max_books_per_page=max_books_per_page,
        stop_key="freewebnovel:ingest",
    )


@router.get(
    "/royalroad",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("5/minute")
def ingest_royalroad(
    request: Request,
    pages: int = Query(1, ge=1, le=10),
    chapters_per_book: int | None = Query(None, ge=0, le=100),
    max_books_per_page: int | None = Query(10, ge=1, le=100),
):

    return _run_webnovel_ingest(
        "royalroad",
        pages_per_source=pages,
        max_public_chapters=chapters_per_book,
        max_books_per_page=max_books_per_page,
        stop_key="royalroad:ingest",
    )


@router.get(
    "/all",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("2/minute")
def ingest_all(request: Request):

    anystories = _run_anystories_ingest()
    alphanovel = _run_alphanovel_ingest()
    webnovels = _run_all_webnovel_ingests()

    return {
        "status": "success",
        "anystories": anystories["count"],
        "alphanovel": alphanovel["count"],
        "webnovels": webnovels["count"],
        "webnovel_sources": {
            source: result["count"]
            for source, result in webnovels["sources"].items()
        },
        "total": (
            anystories["count"]
            + alphanovel["count"]
            + webnovels["count"]
        ),
    }


# =========================================================
# BOOKS API
# =========================================================

@router.get("/books")
@limiter.limit("60/minute")
def get_books(
    request: Request,
    q: str = Query(None),
    genre: str = Query(None),
    source: str = Query(None),
    limit: int = Query(1000, ge=1, le=5000),
):

    with get_db() as db:

        query = db.query(Book)

        if q:

            term = f"%{q}%"

            query = query.filter(
                or_(
                    Book.title.ilike(term),
                    Book.author.ilike(term),
                    Book.genre.ilike(term),
                )
            )

        if genre:
            query = query.filter(
                Book.genre.ilike(
                    f"%{genre}%"
                )
            )

        if source:
            query = query.filter(
                Book.source == source
            )

        books = query.order_by(Book.id.desc()).limit(limit).all()

    return [
        {
            "id": b.id,
            "title": b.title,
            "author": b.author,
            "genre": b.genre,
            "cover": b.cover,
            "download": b.download,
            "source": b.source,
            "chapters": getattr(
                b,
                "chapters_count",
                None,
            ),
            "has_content": bool(
                getattr(
                    b,
                    "chapter_content",
                    None,
                )
            ),
            "cached_chapters": _cached_chapter_count(
                getattr(
                    b,
                    "chapter_content",
                    None,
                )
            ),
            "synopsis": getattr(
                b,
                "synopsis",
                None,
            ),
        }
        for b in books
    ]


@router.get("/books/{book_id}/chapters/{chapter_number}")
@limiter.limit("120/minute")
def get_book_chapter(
    request: Request,
    book_id: int,
    chapter_number: int,
):

    if chapter_number < 1:
        raise HTTPException(
            status_code=400,
            detail="Chapter number must be greater than zero",
        )

    with get_db() as db:

        book = db.query(Book).filter(
            Book.id == book_id
        ).first()

        if not book:
            raise HTTPException(
                status_code=404,
                detail="Book not found",
            )

        chapters = _load_chapters_from_book(book)

        index = chapter_number - 1

        if 0 <= index < len(chapters):
            chapter = chapters[index]

            if isinstance(chapter, dict) and chapter.get("html"):
                return {
                    "book_id": book.id,
                    "chapter": chapter_number,
                    "title": chapter.get("title") or f"Chapter {chapter_number}",
                    "html": chapter.get("html") or "",
                    "available": True,
                    "cached": True,
                    "source": book.source,
                }

        fetched_chapter = _fetch_chapter_from_source(
            book,
            chapter_number,
        )

        if fetched_chapter and fetched_chapter.get("html"):
            _store_chapter(
                db,
                book,
                chapter_number,
                fetched_chapter,
            )

            return {
                "book_id": book.id,
                "chapter": chapter_number,
                "title": fetched_chapter.get("title") or f"Chapter {chapter_number}",
                "html": fetched_chapter.get("html") or "",
                "available": True,
                "cached": False,
                "source": book.source,
            }

        return {
            "book_id": book.id,
            "chapter": chapter_number,
            "title": f"Chapter {chapter_number}",
            "html": (
                "<p>This chapter is not available in PickBook yet. "
                "Please try another chapter while we expand the library.</p>"
            ),
            "available": False,
            "cached": False,
            "source": book.source,
        }
