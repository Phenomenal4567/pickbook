from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager

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

USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "false").lower() == "true"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124 Safari/537.36"
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
# PLAYWRIGHT IMPORT
# =========================================================

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False


# =========================================================
# HELPERS
# =========================================================

def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _pick_img_src(img_tag) -> str | None:
    """
    Extract the real image URL from an <img> tag, handling lazy-load patterns.
    Checks data-src, data-lazy-src, srcset, then falls back to src.
    Skips tiny placeholder GIFs / base64 blobs.
    """
    if img_tag is None:
        return None

    for attr in ("data-src", "data-lazy-src", "data-original"):
        val = img_tag.get(attr, "").strip()
        if val and val.startswith("http") and not val.endswith(".gif"):
            return val

    # srcset — take the first URL
    srcset = img_tag.get("srcset", "").strip()
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first.startswith("http") and not first.endswith(".gif"):
            return first

    src = img_tag.get("src", "").strip()
    # skip base64 blobs and tiny placeholders
    if src and src.startswith("http") and not src.startswith("data:"):
        return src

    return None


def save_books(db, books: list[dict]) -> int:
    existing = {x[0] for x in db.query(Book.download).all()}
    count = 0

    for b in books:
        if not b.get("download"):
            continue
        if b["download"] in existing:
            continue

        # FIX: use getattr-safe field access; Book model must have synopsis column
        # If your Book model doesn't have synopsis yet, add:
        #   synopsis = Column(Text, nullable=True)
        book = Book(
            source=b.get("source", "unknown"),
            title=(b.get("title") or "Untitled")[:255],
            author=(b.get("author") or "Unknown Author")[:255],
            genre=b.get("genre", "Unknown"),
            cover=b.get("cover"),         # nullable — may be None
            synopsis=b.get("synopsis"),   # nullable — may be None
            download=b["download"],
            language="en",
        )

        db.add(book)
        existing.add(b["download"])
        count += 1

    db.commit()
    return count


# =========================================================
# OBOOKO
# =========================================================

OBOOKO_CATEGORIES = [
    ("https://www.obooko.com/category/free-romance-books",          "Romance"),
    ("https://www.obooko.com/category/free-fantasy-books",          "Fantasy"),
    ("https://www.obooko.com/category/free-science-fiction-books",  "Science Fiction"),
    ("https://www.obooko.com/category/free-horror-supernatural-books", "Horror"),
    ("https://www.obooko.com/category/crime-thriller-mystery-books", "Thriller"),
    ("https://www.obooko.com/category/free-historical-fiction-books", "Historical Fiction"),
]

# Obooko book-card selectors (update if site structure changes)
# Each book is in an <article> or a <div> with a link containing the slug pattern
# Obooko URL pattern: /free-books/<slug> or /books/<slug>
_OBOOKO_BOOK_PATH_RE = re.compile(r"/(free-books|books)/[a-z0-9\-]+", re.IGNORECASE)


def _parse_obooko_soup(soup: BeautifulSoup, genre: str) -> list[dict]:
    """
    Parse Obooko category page HTML into a list of book dicts.

    Strategy (robust to layout changes):
      1. Find all <a> tags whose href matches the book-path pattern.
      2. Walk up to the nearest card container to extract cover + author.
      3. Title: prefer heading tags inside the card, fall back to <a> text.
      4. Cover: find <img> inside the card, resolve lazy-load attrs.
      5. Dedup by download URL within this batch.
    """
    seen: set[str] = set()
    books: list[dict] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()

        # Normalise to absolute URL
        if href.startswith("/"):
            href = "https://www.obooko.com" + href
        if not href.startswith("https://www.obooko.com"):
            continue

        path = href.replace("https://www.obooko.com", "")
        if not _OBOOKO_BOOK_PATH_RE.match(path):
            continue  # nav / category / pagination links

        if href in seen:
            continue
        seen.add(href)

        # Walk up from <a> to the card container (up to 4 levels)
        card = a
        for _ in range(4):
            parent = card.parent
            if parent is None:
                break
            # Stop at article / li / div that looks like a card
            if parent.name in ("article", "li") or (
                parent.name == "div" and parent.get("class")
            ):
                card = parent
                break
            card = parent

        # ── Title ──────────────────────────────────────────────────────────
        title = ""
        for tag in ("h2", "h3", "h4", "span", "p"):
            el = card.find(tag)
            if el:
                t = normalize_text(el.get_text())
                if 3 <= len(t) <= 150:
                    title = t
                    break
        if not title:
            title = normalize_text(a.get_text())
        if not title or len(title) < 2:
            continue

        # ── Author ─────────────────────────────────────────────────────────
        author = "Unknown"
        # Obooko often has "by Author Name" in a <p> or <span>
        for el in card.find_all(["p", "span", "div"]):
            t = normalize_text(el.get_text())
            m = re.match(r"^[Bb]y\s+(.{3,60})$", t)
            if m:
                author = m.group(1).strip()
                break

        # ── Cover ──────────────────────────────────────────────────────────
        cover = _pick_img_src(card.find("img"))

        books.append({
            "source": "obooko",
            "title": title[:200],
            "author": author[:150],
            "genre": genre,
            "cover": cover,
            "synopsis": None,
            "download": href,
        })

    return books


def scrape_obooko_static(url: str, genre: str) -> list[dict]:
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    return _parse_obooko_soup(soup, genre)


def scrape_obooko_playwright(url: str, genre: str) -> list[dict]:
    if not PLAYWRIGHT_AVAILABLE:
        return scrape_obooko_static(url, genre)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")

        # Scroll down to trigger lazy-loaded images
        for _ in range(25):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(1200)

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    return _parse_obooko_soup(soup, genre)


# =========================================================
# NOVELFLOW
# =========================================================

NF_GENRES = [
    ("romance",     "Romance"),
    ("fantasy",     "Fantasy"),
    ("mafia",       "Thriller"),
    ("paranormal",  "Paranormal"),
    ("sci-fi",      "Science Fiction"),
    ("vampire",     "Horror"),
    ("ya-teen",     "Young Adult"),
]

_NF_NOVEL_PATH_RE = re.compile(r"^/novel/[^/]+", re.IGNORECASE)


def _parse_nf_soup(soup: BeautifulSoup, genre: str) -> list[dict]:
    """
    Parse NovelFlow story-listing page HTML into book dicts.

    NovelFlow is a Next.js SSR app. In the static HTML pass the novel cards
    are rendered server-side so content IS present in the markup — but images
    use Next.js <Image> which outputs a data URL placeholder in src and stores
    the real URL in data-src / srcset.

    Strategy:
      1. Find all <a href="/novel/..."> — these are the book links.
      2. Walk up to the card container.
      3. Title: look for heading or the first meaningful text node.
      4. Author: look for a sub-heading or "by …" text.
      5. Cover: pick img with lazy-load attrs first.
    """
    seen: set[str] = set()
    books: list[dict] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()
        if not _NF_NOVEL_PATH_RE.match(href):
            continue

        full_url = "https://www.novelflow.app" + href
        if full_url in seen:
            continue
        seen.add(full_url)

        # Walk up to the card (up to 5 levels)
        card = a
        for _ in range(5):
            parent = card.parent
            if parent is None:
                break
            if parent.name in ("article", "li", "section") or (
                parent.name == "div" and parent.get("class")
            ):
                card = parent
                break
            card = parent

        # ── Title ──────────────────────────────────────────────────────────
        title = ""
        for tag in ("h2", "h3", "h4", "h1"):
            el = card.find(tag)
            if el:
                t = normalize_text(el.get_text())
                if 2 <= len(t) <= 200:
                    title = t
                    break

        # Fallback: first meaningful text child of the <a> itself
        if not title:
            for child in a.children:
                t = normalize_text(str(child) if hasattr(child, "__str__") else child)
                if 2 <= len(t) <= 200 and "<" not in t:
                    title = t
                    break

        if not title:
            title = normalize_text(a.get_text(separator=" "))

        title = title[:200].strip()
        if len(title) < 2:
            continue

        # ── Author ─────────────────────────────────────────────────────────
        author = "Unknown"
        for el in card.find_all(["p", "span", "small", "div"]):
            t = normalize_text(el.get_text())
            m = re.match(r"^[Bb]y\s+(.{2,80})$", t)
            if m:
                author = m.group(1).strip()
                break

        # ── Cover ──────────────────────────────────────────────────────────
        # FIX: next/image renders a tiny base64 blur placeholder in `src`.
        # The real URL is in `data-src`, or inside the `srcset` attribute,
        # or in a CSS background-image on a sibling element.
        cover = None
        img = card.find("img")
        if img:
            cover = _pick_img_src(img)

        # If still no cover, check for CSS background-image on any div
        if not cover:
            for div in card.find_all("div", style=True):
                style: str = div.get("style", "")
                m = re.search(r"background-image\s*:\s*url\(['\"]?([^'\")\s]+)['\"]?\)", style)
                if m:
                    url_val = m.group(1)
                    if url_val.startswith("http"):
                        cover = url_val
                        break

        # ── Synopsis ───────────────────────────────────────────────────────
        synopsis = None
        for el in card.find_all(["p"]):
            t = normalize_text(el.get_text())
            if len(t) > 40:
                synopsis = t[:500]
                break

        books.append({
            "source": "novelflow",
            "title": title,
            "author": author[:150],
            "genre": genre,
            "cover": cover,
            "synopsis": synopsis,
            "download": full_url,
        })

    return books


def scrape_nf_static(slug: str, genre: str) -> list[dict]:
    url = f"https://www.novelflow.app/stories/{slug}"
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    return _parse_nf_soup(soup, genre)


def scrape_nf_playwright(slug: str, genre: str) -> list[dict]:
    if not PLAYWRIGHT_AVAILABLE:
        return scrape_nf_static(slug, genre)

    url = f"https://www.novelflow.app/stories/{slug}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")

        # Scroll aggressively to trigger lazy image loading
        for _ in range(40):
            page.mouse.wheel(0, 7000)
            page.wait_for_timeout(1500)

        # Wait for images to finish loading
        page.wait_for_timeout(2000)

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    return _parse_nf_soup(soup, genre)


# =========================================================
# INGEST ROUTES
# =========================================================

# ── Core logic extracted so ingest_all can call them without re-triggering the
#    rate-limit decorator (calling a @limiter.limit-decorated function from inside
#    another request handler triggers a second rate-limit check and can 429).
def _run_obooko_ingest() -> dict:
    scrape = scrape_obooko_playwright if USE_PLAYWRIGHT else scrape_obooko_static
    total = 0

    with get_db() as db:
        for url, genre in OBOOKO_CATEGORIES:
            try:
                books = scrape(url, genre)
                added = save_books(db, books)
                total += added
                print(f"[obooko] {genre}: scraped={len(books)} added={added}")
            except Exception as e:
                print(f"[obooko ERROR] {genre}: {e}")
            time.sleep(1.5)

    return {
        "status": "success",
        "source": "obooko",
        "count": total,
        "mode": "playwright" if USE_PLAYWRIGHT else "static",
    }


def _run_novelflow_ingest() -> dict:
    scrape = scrape_nf_playwright if USE_PLAYWRIGHT else scrape_nf_static
    total = 0

    with get_db() as db:
        for slug, genre in NF_GENRES:
            try:
                books = scrape(slug, genre)
                added = save_books(db, books)
                total += added
                print(f"[novelflow] {genre}: scraped={len(books)} added={added}")
            except Exception as e:
                print(f"[novelflow ERROR] {genre}: {e}")
            time.sleep(1.5)

    return {
        "status": "success",
        "source": "novelflow",
        "count": total,
        "mode": "playwright" if USE_PLAYWRIGHT else "static",
    }


@router.get("/obooko", dependencies=[Depends(_require_admin)])
@limiter.limit("5/minute")
def ingest_obooko(request: Request):
    return _run_obooko_ingest()


@router.get("/novelflow", dependencies=[Depends(_require_admin)])
@limiter.limit("5/minute")
def ingest_novelflow(request: Request):
    return _run_novelflow_ingest()


@router.get("/all", dependencies=[Depends(_require_admin)])
@limiter.limit("2/minute")
def ingest_all(request: Request):
    # FIX: call the plain helper functions instead of the decorated endpoint
    # functions — avoids double rate-limit checks and JSONResponse wrapping issues.
    o = _run_obooko_ingest()
    n = _run_novelflow_ingest()

    return {
        "status": "success",
        "obooko": o["count"],
        "novelflow": n["count"],
        "total": o["count"] + n["count"],
    }


# =========================================================
# BOOKS API  — FIX: include synopsis, subjects, license
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
            query = query.filter(Book.genre.ilike(f"%{genre}%"))

        if source:
            query = query.filter(Book.source == source)

        books = query.limit(500).all()

    return [
        {
            "id":       b.id,
            "title":    b.title,
            "author":   b.author,
            "genre":    b.genre,
            # FIX: return cover — the frontend's coverHtml() renders it with onerror fallback
            "cover":    b.cover,
            "download": b.download,
            "source":   b.source,
            # FIX: these were missing — frontend uses them for mood/tag inference
            "synopsis": getattr(b, "synopsis", None),
            "subjects": getattr(b, "subjects", None) or "",
            "license":  getattr(b, "license",  None) or "Public Domain",
        }
        for b in books
    ]


# =========================================================
# DEBUG
# =========================================================

@router.get("/debug-books", dependencies=[Depends(_require_admin)])
def debug_books(request: Request):
    with get_db() as db:
        books = db.query(Book).limit(20).all()

    return [
        {
            "id":      b.id,
            "title":   b.title,
            "author":  b.author,
            "genre":   b.genre,
            "source":  b.source,
            "cover":   b.cover,
            "synopsis": getattr(b, "synopsis", None),
        }
        for b in books
    ]