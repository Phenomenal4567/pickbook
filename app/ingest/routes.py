"""
app/ingest/routes.py — Dual-source ingestion router
=====================================================

Sources
-------
  Obooko    — https://www.obooko.com    (downloadable EPUB/PDF; public domain & free fiction)
  NovelFlow — https://www.novelflow.app (web-serial novels; cover art + rich metadata)

JS-wall note
------------
Both sites paginate client-side via JavaScript. A plain HTTP GET always
returns the same first-page HTML regardless of ?page= params.

Two scrape strategies are provided for each source:

  Strategy A — Static scrape (default, no extra deps)
    Fetches each category/genre page once.
    Obooko:    ~17 books  per category  (~600–700 total across all categories)
    NovelFlow: ~20 novels per genre     (~260–300 total across all genres)

  Strategy B — Playwright full crawl (USE_PLAYWRIGHT=true)
    Headless Chromium; scrolls / clicks "Load More" until exhausted.
    Requires: pip install playwright && playwright install chromium

Environment variables
---------------------
  USE_PLAYWRIGHT      = true | false (default: false)
  NF_ENRICH_DETAILS   = true | false (default: false)
      When true, every NovelFlow novel URL is fetched individually for
      accurate author + tag metadata.  Very slow; prefer Playwright instead.
  ADMIN_TOKEN         = <secret>   Required for all admin endpoints.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from bs4 import BeautifulSoup
import requests
from requests.exceptions import RequestException, SSLError

from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.limiter import limiter
from app.models.book import Book
from starlette.requests import Request

router = APIRouter(prefix="/ingest", tags=["Ingestion"])

USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "false").lower() == "true"


# =========================================================
# ADMIN AUTH DEPENDENCY
# Mirrors verify_admin in main.py.  All ingest + debug
# endpoints require a valid X-Admin-Token header.
# Uses settings.admin_token (loaded from .env via pydantic-settings)
# rather than os.getenv so the value is always consistent with the
# rest of the app.
# =========================================================

def _require_admin(x_admin_token: str = Header(None)) -> None:
    expected = settings.admin_token
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_TOKEN is not configured on the server.",
        )
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing admin token.",
        )


# =========================================================
# DB SESSION
# =========================================================

@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================================================
# SHARED HTTP CONFIG
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_DELAY = 1.5   # seconds between requests
MAX_RETRIES   = 3
RETRY_BACKOFF = 2.0


def _fetch_with_retry(url: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(url, headers=HEADERS, timeout=30)
            if res.status_code < 500:
                res.raise_for_status()
                return res
            last_exc = RequestException(f"Server error {res.status_code}")
        except SSLError as exc:
            last_exc = exc
        except RequestException:
            raise
        wait = RETRY_BACKOFF * (2 ** attempt)
        print(
            f"[ingest] Retrying {url} in {wait:.0f}s "
            f"(attempt {attempt + 1}/{MAX_RETRIES}): {last_exc}"
        )
        time.sleep(wait)
    raise RequestException(
        f"All {MAX_RETRIES} attempts failed for {url}: {last_exc}"
    )


# =========================================================
# ─────────────────────────────────────────────────────────
#  OBOOKO  SOURCE
# ─────────────────────────────────────────────────────────
# =========================================================

OBOOKO_BASE               = "https://www.obooko.com"
OBOOKO_ALL_CATEGORIES_URL = f"{OBOOKO_BASE}/all-categories"

OBOOKO_BLOCKED_HREF_FRAGMENTS = [
    "?sort=", "?page=", "?format=", "?rating=",
    "/all-categories",
    "/free-fiction-menu", "/free-nonfiction-menu", "/free-staff-picks-menu",
    "/free-popular-books", "/free-staff-picks",
    "menu", "#", "javascript:", "/blog-post/",
]

# Blocked path prefixes — category listing pages, blog, nav
_OBOOKO_BLOCKED_PATH_PREFIXES = (
    "/category/",
    "/blog-post/",
    "/free-popular-books",
    "/free-staff-picks",
)

# Book page: exactly two path segments /<category-slug>/<book-slug>
# Negative lookahead excludes /category/... listing pages
_OBOOKO_BOOK_PATH_RE     = re.compile(r"^/(?!category/)[a-z0-9-]+/[a-z0-9][a-z0-9-]*$")
_OBOOKO_CATEGORY_PATH_RE = re.compile(r"^/category/[a-z0-9-]+$")

# Strip trailing rating — handles both formats:
#   "5.0 (93)"  — space before rating
#   "4.0(43)"   — no space (Obooko's current HTML)
_OBOOKO_RATING_RE = re.compile(r"\s*\d+\.\d+\s*(\(\d+\))?$")

OBOOKO_GENRE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(romance|romantic|love story)\b"),                        "Romance"),
    (re.compile(r"\b(fantasy|dragon|magic|wizard|witch)\b"),                  "Fantasy"),
    (re.compile(r"\b(science[\s-]fiction|sci[\s-]fi|spaceship|alien)\b"),     "Science Fiction"),
    (re.compile(r"\b(horror|supernatural|ghost|haunted|vampire|zombie)\b"),   "Horror"),
    (re.compile(r"\b(thriller|mystery|crime|detective|murder)\b"),            "Thriller"),
    (re.compile(r"\b(historical[\s-]fiction|historical|history|medieval)\b"), "Historical Fiction"),
    (re.compile(r"\b(biography|memoir|autobiography|non[\s-]fiction)\b"),     "Non Fiction"),
    (re.compile(r"\b(classic|classics)\b"),                                   "Classic"),
    (re.compile(r"\b(poetry|poem|poems)\b"),                                  "Poetry"),
    (re.compile(r"\b(children|teen|young adult)\b"),                          "Young Adult"),
]

_OBOOKO_SLUG_GENRE_MAP: dict[str, str] = {
    "romance":             "Romance",
    "romantic":            "Romance",
    "love-story":          "Romance",
    "billionaire-romance": "Romance",
    "dark-romance":        "Romance",
    "steamy-romance":      "Romance",
    "fantasy":             "Fantasy",
    "science-fiction":     "Science Fiction",
    "horror":              "Horror",
    "thriller":            "Thriller",
    "mystery":             "Thriller",
    "crime":               "Thriller",
    "historical-fiction":  "Historical Fiction",
    "history":             "Historical Fiction",
    "memoir":              "Non Fiction",
    "biography":           "Non Fiction",
    "classic":             "Classic",
    "poetry":              "Poetry",
    "teen":                "Young Adult",
    "young-adult":         "Young Adult",
    "action":              "Action & Adventure",
    "adventure":           "Action & Adventure",
}


# ── Obooko helpers ────────────────────────────────────────

def _obooko_genre_from_url(url: str) -> str | None:
    path = url.replace(OBOOKO_BASE, "").lower()
    for slug, genre in _OBOOKO_SLUG_GENRE_MAP.items():
        if slug in path:
            return genre
    return None


def _obooko_is_book_page(href: str) -> bool:
    if not href.startswith(OBOOKO_BASE):
        return False
    path = href[len(OBOOKO_BASE):].split("?")[0]
    # Block category listing pages, blog posts, and nav links by prefix
    if any(path.startswith(p) for p in _OBOOKO_BLOCKED_PATH_PREFIXES):
        return False
    # Must match /<category-slug>/<book-slug> exactly
    if not _OBOOKO_BOOK_PATH_RE.match(path):
        return False
    if any(frag in href for frag in OBOOKO_BLOCKED_HREF_FRAGMENTS):
        return False
    return True


def _obooko_normalise_href(raw: str) -> str | None:
    if not raw:
        return None
    href = (OBOOKO_BASE + raw) if raw.startswith("/") else raw
    return href if _obooko_is_book_page(href) else None


def _obooko_detect_genre(href: str, text: str) -> str:
    combined = (href + " " + text).lower()
    for pattern, genre in OBOOKO_GENRE_RULES:
        if pattern.search(combined):
            return genre
    return "Unknown"


def _obooko_dedupe_title(s: str) -> str:
    """Collapse obooko's doubled anchor text: 'Foo Bar Foo Bar' → 'Foo Bar'."""
    n = len(s)
    for half in range(n // 2, 0, -1):
        if s[:half] == s[n - half:]:
            candidate = s[:half].strip()
            if candidate:
                return candidate
    return s


def _obooko_parse_title_author(text: str) -> tuple[str, str] | None:
    """
    Parse Obooko anchor text into (title, author).

    Obooko currently renders anchor text in two formats:
      Old: 'Title Title by Author 5.0 (93)'   — space-padded ' by '
      New: 'TitleTitleby Author4.0(43)'        — no space before 'by', no space before rating

    Steps:
      1. Strip trailing rating  (handles both '5.0 (93)' and '4.0(43)')
      2. Split on last 'by ' — with or without leading space
      3. De-duplicate title   (Obooko doubles anchor text: 'Foo Bar Foo Bar')
      4. Clean up leftover whitespace
    """
    # Step 1 — strip trailing rating (greedy, handles no-space variant)
    text = _OBOOKO_RATING_RE.sub("", text).strip()

    # Step 2 — split on last occurrence of 'by ' (case-sensitive)
    # Use ' by ' first (old format), then fall back to 'by ' (new format)
    idx = text.rfind(" by ")
    if idx != -1:
        raw_title = text[:idx]
        author    = text[idx + 4:].strip()
    else:
        idx = text.rfind("by ")
        if idx != -1:
            raw_title = text[:idx]
            author    = text[idx + 3:].strip()
        else:
            raw_title = text
            author    = "Unknown"

    author = author or "Unknown"

    # Step 3 — de-duplicate doubled title
    title = _obooko_dedupe_title(raw_title.strip())

    # Step 4 — reject empty or suspiciously short titles
    if not title or len(title) < 2:
        return None

    return (title, author)


# ── Obooko category discovery ─────────────────────────────

def _obooko_discover_categories() -> list[str]:
    FALLBACK = [
        f"{OBOOKO_BASE}/category/free-romance-books",
        f"{OBOOKO_BASE}/category/free-fantasy-books",
        f"{OBOOKO_BASE}/category/free-science-fiction-books",
        f"{OBOOKO_BASE}/category/free-horror-supernatural-books",
        f"{OBOOKO_BASE}/category/crime-thriller-mystery-books",
        f"{OBOOKO_BASE}/category/free-historical-fiction-books",
        f"{OBOOKO_BASE}/category/free-memoir-biography-autobiography-books",
        f"{OBOOKO_BASE}/category/free-classic-books",
        f"{OBOOKO_BASE}/category/free-poetry-collections",
    ]
    try:
        res = _fetch_with_retry(OBOOKO_ALL_CATEGORIES_URL)
    except RequestException as exc:
        print(f"[obooko] WARNING: category list fetch failed: {exc}. Using fallback.")
        return FALLBACK

    soup = BeautifulSoup(res.text, "html.parser")
    urls, seen = [], set()
    for a in soup.find_all("a", href=True):
        raw: str = a["href"]
        href = (OBOOKO_BASE + raw) if raw.startswith("/") else raw
        path = href.replace(OBOOKO_BASE, "").split("?")[0]
        if _OBOOKO_CATEGORY_PATH_RE.match(path) and href not in seen:
            urls.append(href)
            seen.add(href)

    print(f"[obooko] Discovered {len(urls)} category URLs.")
    return urls if urls else FALLBACK


# ── Obooko scrape strategies ──────────────────────────────

def _obooko_scrape_static(url: str, genre_override: str | None) -> list[dict]:
    try:
        res = _fetch_with_retry(url)
    except RequestException as exc:
        print(f"[obooko] WARNING: skipping {url}: {exc}")
        return []

    soup  = BeautifulSoup(res.text, "html.parser")
    books: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = _obooko_normalise_href(a["href"])
        if not href:
            continue
        text = a.get_text(strip=True)
        if not text:
            continue
        parsed = _obooko_parse_title_author(text)
        if not parsed:
            continue
        title, author = parsed
        books.append({
            "title":    title,
            "author":   author,
            "genre":    genre_override or _obooko_detect_genre(href, text),
            "cover":    None,
            "download": href,
            "source":   "obooko",
            "language": "en",
        })
    return books


def _obooko_scrape_playwright(url: str, genre_override: str | None) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[obooko] Playwright not installed. Falling back to static scrape.")
        return _obooko_scrape_static(url, genre_override)

    books: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page(extra_http_headers={
            "User-Agent":      HEADERS["User-Agent"],
            "Accept-Language": HEADERS["Accept-Language"],
        })
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            for _ in range(50):
                try:
                    btn = page.locator("text=Load More").first
                    btn.wait_for(state="visible", timeout=3_000)
                    btn.click()
                    page.wait_for_timeout(1_200)
                except PWTimeout:
                    break
            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = _obooko_normalise_href(a["href"])
        if not href:
            continue
        text = a.get_text(strip=True)
        if not text:
            continue
        parsed = _obooko_parse_title_author(text)
        if not parsed:
            continue
        title, author = parsed
        books.append({
            "title":    title,
            "author":   author,
            "genre":    genre_override or _obooko_detect_genre(href, text),
            "cover":    None,
            "download": href,
            "source":   "obooko",
            "language": "en",
        })
    return books


# =========================================================
# ─────────────────────────────────────────────────────────
#  NOVELFLOW  SOURCE
# ─────────────────────────────────────────────────────────
# =========================================================

NF_BASE      = "https://www.novelflow.app"
NF_COVER_CDN = "https://cover-v1.novelflow.app"

# All genre slugs discoverable from NovelFlow's nav bar
# (slug, canonical_genre_label)
NF_GENRE_SLUGS: list[tuple[str, str]] = [
    ("all",           "Unknown"),          # broad sweep; genre resolved per-novel card
    ("romance",       "Romance"),
    ("werewolf",      "Paranormal"),
    ("mafia",         "Thriller"),
    ("fantasy",       "Fantasy"),
    ("lgbtq-",        "LGBTQ+"),
    ("ya-teen",       "Young Adult"),
    ("paranormal",    "Paranormal"),
    ("mystery-crime", "Thriller"),
    ("sci-fi",        "Science Fiction"),
    ("vampire",       "Horror"),
    ("action",        "Action & Adventure"),
    ("other",         "Other"),
]

# Novel URL pattern: /novel/<slug> or /novel/<slug>_<numeric-id>
_NF_NOVEL_PATH_RE = re.compile(r"^/novel/[a-z0-9_-]+$")

# Map NovelFlow tag slugs → canonical genre strings
_NF_TAG_GENRE_MAP: dict[str, str] = {
    "romance":         "Romance",
    "dark-romance":    "Romance",
    "contemporary":    "Romance",
    "fantasy-romance": "Romance",
    "fantasy":         "Fantasy",
    "sci-fi":          "Science Fiction",
    "horror":          "Horror",
    "supernatural":    "Horror",
    "vampire":         "Horror",
    "werewolf":        "Paranormal",
    "paranormal":      "Paranormal",
    "mystery-crime":   "Thriller",
    "thriller":        "Thriller",
    "mafia":           "Thriller",
    "action":          "Action & Adventure",
    "adventure":       "Action & Adventure",
    "lgbtq+":          "LGBTQ+",
    "lgbtq-":          "LGBTQ+",
    "ya-teen":         "Young Adult",
    "young-adult":     "Young Adult",
    "teen":            "Young Adult",
    "biography":       "Non Fiction",
    "non-fiction":     "Non Fiction",
    "poetry":          "Poetry",
    "classic":         "Classic",
}


# ── NovelFlow helpers ─────────────────────────────────────

def _nf_is_novel_url(href: str) -> bool:
    if not href.startswith(NF_BASE):
        return False
    path = href[len(NF_BASE):].split("?")[0]
    return bool(_NF_NOVEL_PATH_RE.match(path))


def _nf_normalise_href(raw: str) -> str | None:
    if not raw:
        return None
    href = (NF_BASE + raw) if raw.startswith("/") else raw
    return href if _nf_is_novel_url(href) else None


def _nf_resolve_genre(tags: list[str], fallback: str) -> str:
    """Return the first matching genre from a novel's tag list."""
    for tag in tags:
        slug = tag.lower().strip()
        if slug in _NF_TAG_GENRE_MAP:
            return _NF_TAG_GENRE_MAP[slug]
    return fallback


def _nf_fetch_novel_detail(url: str) -> dict | None:
    """
    Fetch a single NovelFlow novel detail page and extract:
      title, author, genre (from tags), cover URL.
    Returns None if the page cannot be parsed.

    Only called when NF_ENRICH_DETAILS=true.
    """
    try:
        res = _fetch_with_retry(url)
    except RequestException as exc:
        print(f"[novelflow] WARNING: skipping detail {url}: {exc}")
        return None

    soup = BeautifulSoup(res.text, "html.parser")

    # Title — <h1>
    h1    = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    if not title:
        return None

    # Author — <a href="/author/<id>">
    author    = "Unknown"
    author_a  = soup.find("a", href=re.compile(r"^/author/"))
    if author_a:
        author = author_a.get_text(strip=True).replace("Author:", "").strip()

    # Cover — first <img> from the cover CDN
    cover: str | None = None
    for img in soup.find_all("img", src=True):
        src: str = img["src"]
        if NF_COVER_CDN in src:
            cover = src.split("?")[0]   # strip resize query params
            break

    # Tags → genre
    tag_links = soup.find_all("a", href=re.compile(r"^/tag/"))
    tags      = [a.get_text(strip=True) for a in tag_links]
    genre     = _nf_resolve_genre(tags, "Unknown")

    return {
        "title":    title,
        "author":   author,
        "genre":    genre,
        "cover":    cover,
        "download": url,
        "source":   "novelflow",
        "language": "en",
    }


# ── NovelFlow scrape strategies ───────────────────────────

def _nf_scrape_genre_static(genre_slug: str, genre_label: str) -> list[dict]:
    """
    Fetch /stories/<genre_slug> once (static HTML).
    Extracts novel URLs + titles + cover images from the listing page.

    If NF_ENRICH_DETAILS=true, each URL is fetched again individually
    for accurate author + tag/genre data (much slower).
    """
    url = f"{NF_BASE}/stories/{genre_slug}"
    try:
        res = _fetch_with_retry(url)
    except RequestException as exc:
        print(f"[novelflow] WARNING: skipping genre '{genre_slug}': {exc}")
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    novels: list[dict] = []
    seen_hrefs: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = _nf_normalise_href(a["href"])
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        # Title: prefer the anchor's title attr, fallback to text
        title = (a.get("title") or a.get_text(strip=True)).strip()
        if not title:
            continue

        # Cover: <img> inside the anchor card whose src is on the CDN
        cover: str | None = None
        img = a.find("img", src=True)
        if img and NF_COVER_CDN in img.get("src", ""):
            cover = img["src"].split("?")[0]

        novels.append({
            "title":    title,
            "author":   "Unknown",   # enriched below if NF_ENRICH_DETAILS=true
            "genre":    genre_label,
            "cover":    cover,
            "download": href,
            "source":   "novelflow",
            "language": "en",
        })

    # Optional per-novel detail enrichment
    if os.getenv("NF_ENRICH_DETAILS", "false").lower() == "true":
        enriched: list[dict] = []
        for novel in novels:
            detail = _nf_fetch_novel_detail(novel["download"])
            enriched.append(detail if detail else novel)
            time.sleep(REQUEST_DELAY)
        return enriched

    return novels


def _nf_scrape_genre_playwright(genre_slug: str, genre_label: str) -> list[dict]:
    """
    Playwright strategy: scroll to the bottom repeatedly until no new
    content loads, then parse all novel cards.
    Falls back to static scrape if Playwright is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[novelflow] Playwright not installed. Falling back to static scrape.")
        return _nf_scrape_genre_static(genre_slug, genre_label)

    url = f"{NF_BASE}/stories/{genre_slug}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page(extra_http_headers={
            "User-Agent":      HEADERS["User-Agent"],
            "Accept-Language": HEADERS["Accept-Language"],
        })
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # NovelFlow uses infinite scroll
            for _ in range(30):
                prev_height = page.evaluate("document.body.scrollHeight")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1_500)
                new_height  = page.evaluate("document.body.scrollHeight")
                if new_height == prev_height:
                    break   # page fully loaded
            # Also click any residual "Load More" button
            try:
                btn = page.locator("text=Load More").first
                btn.wait_for(state="visible", timeout=2_000)
                btn.click()
                page.wait_for_timeout(1_200)
            except PWTimeout:
                pass
            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    novels: list[dict] = []
    seen_hrefs: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = _nf_normalise_href(a["href"])
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        title = (a.get("title") or a.get_text(strip=True)).strip()
        if not title:
            continue
        cover: str | None = None
        img = a.find("img", src=True)
        if img and NF_COVER_CDN in img.get("src", ""):
            cover = img["src"].split("?")[0]
        novels.append({
            "title":    title,
            "author":   "Unknown",
            "genre":    genre_label,
            "cover":    cover,
            "download": href,
            "source":   "novelflow",
            "language": "en",
        })

    return novels


# =========================================================
# ─────────────────────────────────────────────────────────
#  SHARED DB HELPER
# ─────────────────────────────────────────────────────────
# =========================================================

def _commit_books(db, books: list[dict], existing_urls: set[str]) -> int:
    """
    Insert new books into the DB session (not yet committed).
    Skips any whose download URL is already in existing_urls.
    Returns the count of rows staged for insert.
    """
    count = 0
    for b in books:
        if b["download"] in existing_urls:
            continue
        db.add(Book(
            source   = b["source"],
            title    = b["title"],
            author   = b["author"],
            genre    = b["genre"],
            cover    = b.get("cover"),
            download = b["download"],
            language = b.get("language", "en"),
        ))
        existing_urls.add(b["download"])
        count += 1
    return count


# =========================================================
# ─────────────────────────────────────────────────────────
#  INGEST ENDPOINTS
# ─────────────────────────────────────────────────────────
# All endpoints:
#   - Require valid X-Admin-Token header (_require_admin)
#   - Rate-limited via slowapi (5 req/min for heavy ingest,
#     2 req/min for the combined /all endpoint)
# ─────────────────────────────────────────────────────────
# =========================================================

@router.delete(
    "/obooko/clean",
    dependencies=[Depends(_require_admin)],
    summary="Delete all obooko rows then re-import with fixed parser",
)
@limiter.limit("2/minute")
def clean_and_reimport_obooko(request: Request):
    """
    Wipes every row where source='obooko' then runs a fresh
    ingest with the corrected title/author parser and URL filter.
    Use this once to replace the dirty initial import.
    """
    try:
        with get_db() as db:
            deleted = db.query(Book).filter(Book.source == "obooko").delete()
            db.commit()
            print(f"[obooko] Deleted {deleted} stale rows.")
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")

    return ingest_obooko(request)


@router.get(
    "/obooko",
    dependencies=[Depends(_require_admin)],
    summary="Scrape Obooko and import into DB",
)
@limiter.limit("5/minute")
def ingest_obooko(request: Request):
    """
    Discovers all Obooko category pages, scrapes each one,
    and upserts books into the DB (deduped by download URL).
    """
    scrape_fn     = _obooko_scrape_playwright if USE_PLAYWRIGHT else _obooko_scrape_static
    category_urls = _obooko_discover_categories()
    total_count   = 0

    try:
        with get_db() as db:
            existing_urls: set[str] = {
                row[0] for row in db.query(Book.download).all()
            }
            for cat_url in category_urls:
                genre_override = _obooko_genre_from_url(cat_url)
                raw_books      = scrape_fn(cat_url, genre_override)
                page_count     = _commit_books(db, raw_books, existing_urls)
                try:
                    db.commit()
                    total_count += page_count
                    print(f"[obooko] {cat_url} → {page_count} new books")
                except SQLAlchemyError as exc:
                    db.rollback()
                    print(f"[obooko] WARNING: DB commit failed for {cat_url}: {exc}")
                time.sleep(REQUEST_DELAY)

    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    mode = "playwright" if USE_PLAYWRIGHT else "static"
    return {"status": "obooko imported", "mode": mode, "count": total_count}


@router.get(
    "/novelflow",
    dependencies=[Depends(_require_admin)],
    summary="Scrape NovelFlow and import into DB",
)
@limiter.limit("5/minute")
def ingest_novelflow(request: Request):
    """
    Iterates over all NovelFlow genre slugs, scrapes each listing page,
    and upserts novels into the DB (deduped by download URL).

    Cover images are stored as CDN URLs (cover-v1.novelflow.app/...).
    Set NF_ENRICH_DETAILS=true to fetch each novel's detail page for
    richer author + genre metadata (much slower; Playwright is preferred).
    """
    scrape_fn   = _nf_scrape_genre_playwright if USE_PLAYWRIGHT else _nf_scrape_genre_static
    total_count = 0

    # When enriching detail pages, skip "all" to avoid re-importing
    # novels already captured by the individual genre passes
    genres_to_scrape = (
        [(s, l) for s, l in NF_GENRE_SLUGS if s != "all"]
        if os.getenv("NF_ENRICH_DETAILS", "false").lower() == "true"
        else NF_GENRE_SLUGS
    )

    try:
        with get_db() as db:
            existing_urls: set[str] = {
                row[0] for row in db.query(Book.download).all()
            }
            for genre_slug, genre_label in genres_to_scrape:
                raw_novels = scrape_fn(genre_slug, genre_label)
                page_count = _commit_books(db, raw_novels, existing_urls)
                try:
                    db.commit()
                    total_count += page_count
                    print(f"[novelflow] /stories/{genre_slug} → {page_count} new novels")
                except SQLAlchemyError as exc:
                    db.rollback()
                    print(f"[novelflow] WARNING: DB commit failed for '{genre_slug}': {exc}")
                time.sleep(REQUEST_DELAY)

    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    mode = "playwright" if USE_PLAYWRIGHT else "static"
    return {"status": "novelflow imported", "mode": mode, "count": total_count}


@router.get(
    "/all",
    dependencies=[Depends(_require_admin)],
    summary="Run Obooko + NovelFlow ingestion in sequence",
)
@limiter.limit("2/minute")
def ingest_all(request: Request):
    """
    Convenience endpoint: runs Obooko then NovelFlow ingestion back-to-back.
    Returns a combined summary with per-source counts.
    """
    obooko_result = ingest_obooko(request)
    nf_result     = ingest_novelflow(request)
    return {
        "status":    "all sources imported",
        "obooko":    obooko_result,
        "novelflow": nf_result,
        "total":     obooko_result["count"] + nf_result["count"],
    }


# =========================================================
# ─────────────────────────────────────────────────────────
#  QUERY ENDPOINTS
# ─────────────────────────────────────────────────────────
# =========================================================

@router.get(
    "/books",
    summary="Search books across all sources",
)
@limiter.limit("60/minute")
def get_books(
    request: Request,
    q:      str = Query(None, max_length=200, description="Search title, author, or genre"),
    source: str = Query(None, description="Filter by source: 'obooko' or 'novelflow'"),
    genre:  str = Query(None, max_length=100, description="Filter by genre"),
):
    with get_db() as db:
        query = db.query(Book)

        if q:
            term  = f"%{q}%"
            query = query.filter(
                or_(
                    Book.title.ilike(term),
                    Book.author.ilike(term),
                    Book.genre.ilike(term),
                )
            )
        if source:
            query = query.filter(Book.source == source)

        if genre:
            query = query.filter(Book.genre.ilike(f"%{genre}%"))

        books = query.all()

    return [
        {
            "id":       b.id,
            "source":   b.source,
            "title":    b.title,
            "author":   b.author,
            "genre":    b.genre,
            "cover":    b.cover,
            "download": b.download,
        }
        for b in books
    ]


# =========================================================
# DEBUG  (admin-token protected; hidden from docs in prod)
# =========================================================

@router.get(
    "/debug-books",
    dependencies=[Depends(_require_admin)],
    summary="Return first 20 books from DB (dev/debug only)",
    include_in_schema=True,   # docs_url=None in prod hides this automatically
)
@limiter.limit("10/minute")
def debug_books(request: Request):
    with get_db() as db:
        books = db.query(Book).limit(20).all()

    return [
        {
            "source":   b.source,
            "title":    b.title,
            "author":   b.author,
            "genre":    b.genre,
            "cover":    b.cover,
            "download": b.download,
        }
        for b in books
    ]