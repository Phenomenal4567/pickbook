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
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.limiter import limiter
from app.models.book import Book

router = APIRouter(prefix="/ingest", tags=["Ingestion"])

USE_PLAYWRIGHT = settings.use_playwright

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


def _pick_img_src(img_tag) -> str | None:
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
            value = urljoin(ALPHA_BASE_URL, value)

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


def save_books(db, books: list[dict]) -> int:

    existing = {
        book.download: book
        for book in db.query(Book).all()
    }

    count = 0

    for b in books:

        if not b.get("download"):
            continue

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

                should_replace = (
                    b.get("source") == "anystories"
                    and is_bad_listing_title
                    and field in ("cover", "synopsis")
                )

                if new_value and (
                    should_replace
                    or not getattr(existing_book, field, None)
                ):
                    setattr(existing_book, field, new_value)
                    changed = True

            if changed:
                db.add(existing_book)

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

        existing[b["download"]] = book

        count += 1

    db.commit()

    return count


def _bulk_save_books(db, batch: list[dict]) -> int:

    count = save_books(db, batch)

    print(
        f"[bulk_save] batch={len(batch)} saved={count}"
    )

    return count


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
        "\n\n".join(paragraphs[:80])
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
    max_public_chapters: int = 2,
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

    chapters = []

    for chapter_number in range(1, max_public_chapters + 1):
        try:
            chapter_html = fetch(
                f"{detail_url.rstrip('/')}/chapters/{chapter_number}"
            )
        except RequestException as e:
            print(
                f"[anystories chapter ERROR] "
                f"{detail_url} chapter {chapter_number}: {e}"
            )
            continue

        chapter = _parse_as_chapter(
            chapter_html,
            chapter_number,
        )

        if chapter:
            chapters.append(chapter)

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
) -> list[dict]:

    for book in books:
        try:
            detail_html = fetch(book["download"])
            details = _parse_as_details(
                detail_html,
                book["download"],
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

    return len(chapters) if isinstance(chapters, list) else 0


# =========================================================
# PLAYWRIGHT FETCH
# =========================================================

async def _async_fetch_as(slug: str) -> str:

    url = (
        f"https://www.anystories.app/genre/"
        f"{slug}?order=score&page=1"
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
) -> list[dict]:

    if not PLAYWRIGHT_AVAILABLE:

        print(
            "[anystories] Playwright unavailable"
        )

        return scrape_as_static(slug, genre)

    try:

        html = asyncio.run(
            _async_fetch_as(slug)
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
            books
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
) -> list[dict]:

    url = (
        f"https://www.anystories.app/genre/"
        f"{slug}?order=score&page=1"
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
        books
    )


def scrape_alpha_static(
    slug: str,
    genre: str,
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


# =========================================================
# INGEST CORE
# =========================================================

def _run_ingest_pipeline(
    source_name: str,
    genres: list[tuple[str, str]],
    scrape_fn,
    batch_size: int = 50,
) -> dict:

    total = 0

    batch_buffer: list[dict] = []

    with get_db() as db:

        for slug, genre in genres:

            try:

                books = scrape_fn(
                    slug,
                    genre,
                )

                batch_buffer.extend(books)

                print(
                    f"[{source_name}] "
                    f"{genre}: "
                    f"{len(books)} scraped"
                )

                if (
                    len(batch_buffer)
                    >= batch_size
                ):

                    added = _bulk_save_books(
                        db,
                        batch_buffer,
                    )

                    total += added

                    batch_buffer = []

            except Exception as e:

                print(
                    f"[{source_name} ERROR] "
                    f"{genre}: {e}"
                )

            delay = random.randint(1, 3)

            time.sleep(delay)

        if batch_buffer:

            added = _bulk_save_books(
                db,
                batch_buffer,
            )

            total += added

    return {
        "status": "success",
        "source": source_name,
        "count": total,
        "mode": (
            "playwright"
            if USE_PLAYWRIGHT
            else "static"
        ),
    }


# =========================================================
# INGEST HELPERS
# =========================================================

def _run_anystories_ingest():

    if USE_PLAYWRIGHT and PLAYWRIGHT_AVAILABLE:
        scrape_fn = scrape_as_playwright
    else:
        scrape_fn = scrape_as_static

    return _run_ingest_pipeline(
        "anystories",
        AS_GENRES,
        scrape_fn,
    )


def _run_alphanovel_ingest():

    return _run_ingest_pipeline(
        "alphanovel",
        ALPHA_GENRES,
        scrape_alpha_static,
    )


# =========================================================
# ROUTES
# =========================================================

@router.get(
    "/anystories",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("5/minute")
def ingest_anystories(request: Request):

    return _run_anystories_ingest()


@router.get(
    "/alphanovel",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("5/minute")
def ingest_alphanovel(request: Request):

    return _run_alphanovel_ingest()


@router.get(
    "/all",
    dependencies=[Depends(_require_admin)],
)
@limiter.limit("2/minute")
def ingest_all(request: Request):

    anystories = _run_anystories_ingest()
    alphanovel = _run_alphanovel_ingest()

    return {
        "status": "success",
        "anystories": anystories["count"],
        "alphanovel": alphanovel["count"],
        "total": anystories["count"] + alphanovel["count"],
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

        books = query.order_by(Book.id.desc()).limit(500).all()

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

        chapters = []

        if book.chapter_content:
            try:
                chapters = json.loads(book.chapter_content)
            except json.JSONDecodeError:
                chapters = []

        index = chapter_number - 1

        if 0 <= index < len(chapters):
            chapter = chapters[index]

            return {
                "book_id": book.id,
                "chapter": chapter_number,
                "title": chapter.get("title") or f"Chapter {chapter_number}",
                "html": chapter.get("html") or "",
                "available": True,
                "source_url": book.download,
                "source_chapter_url": _source_chapter_url(
                    book,
                    chapter_number,
                ),
            }

        return {
            "book_id": book.id,
            "chapter": chapter_number,
            "title": f"Chapter {chapter_number}",
            "html": (
                "<p>This chapter is not available in the local PickBook "
                "cache yet.</p>"
            ),
            "available": False,
            "source_url": book.download,
            "source_chapter_url": _source_chapter_url(
                book,
                chapter_number,
            ),
        }
