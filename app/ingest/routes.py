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
except:
    PLAYWRIGHT_AVAILABLE = False


# =========================================================
# HELPERS
# =========================================================

def fetch(url: str):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def normalize_text(text: str):
    return re.sub(r"\s+", " ", text).strip()


def save_books(db, books):
    existing = {
        x[0]
        for x in db.query(Book.download).all()
    }

    count = 0

    for b in books:
        if not b.get("download"):
            continue

        if b["download"] in existing:
            continue

        book = Book(
            source=b["source"],
            title=b["title"],
            author=b["author"],
            genre=b["genre"],
            cover=b.get("cover"),
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
    ("https://www.obooko.com/category/free-romance-books", "Romance"),
    ("https://www.obooko.com/category/free-fantasy-books", "Fantasy"),
    ("https://www.obooko.com/category/free-science-fiction-books", "Science Fiction"),
    ("https://www.obooko.com/category/free-horror-supernatural-books", "Horror"),
    ("https://www.obooko.com/category/crime-thriller-mystery-books", "Thriller"),
    ("https://www.obooko.com/category/free-historical-fiction-books", "Historical Fiction"),
]


def scrape_obooko_static(url, genre):
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    books = []

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "/category/" in href:
            continue

        if href.startswith("/"):
            href = "https://www.obooko.com" + href

        if "obooko.com" not in href:
            continue

        text = normalize_text(a.get_text())

        if len(text) < 3:
            continue

        title = text[:120]

        books.append({
            "source": "obooko",
            "title": title,
            "author": "Unknown",
            "genre": genre,
            "cover": None,
            "download": href,
        })

    return books


def scrape_obooko_playwright(url, genre):
    if not PLAYWRIGHT_AVAILABLE:
        return scrape_obooko_static(url, genre)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page()

        page.goto(url, wait_until="networkidle")

        for _ in range(25):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(1200)

        html = page.content()

        browser.close()

    soup = BeautifulSoup(html, "html.parser")

    books = []

    for a in soup.find_all("a", href=True):
        href = a.get("href")

        if not href:
            continue

        if "/category/" in href:
            continue

        if href.startswith("/"):
            href = "https://www.obooko.com" + href

        if "obooko.com" not in href:
            continue

        text = normalize_text(a.get_text())

        if len(text) < 3:
            continue

        books.append({
            "source": "obooko",
            "title": text[:120],
            "author": "Unknown",
            "genre": genre,
            "cover": None,
            "download": href,
        })

    return books


# =========================================================
# NOVELFLOW
# =========================================================

NF_GENRES = [
    ("romance", "Romance"),
    ("fantasy", "Fantasy"),
    ("mafia", "Thriller"),
    ("paranormal", "Paranormal"),
    ("sci-fi", "Science Fiction"),
    ("vampire", "Horror"),
    ("ya-teen", "Young Adult"),
]


def scrape_nf_static(slug, genre):
    url = f"https://www.novelflow.app/stories/{slug}"

    html = fetch(url)

    soup = BeautifulSoup(html, "html.parser")

    books = []

    for a in soup.find_all("a", href=True):

        href = a.get("href")

        if not href:
            continue

        if not href.startswith("/novel/"):
            continue

        full_url = "https://www.novelflow.app" + href

        title = normalize_text(a.get_text())

        if not title:
            title = "Unknown Title"

        img = a.find("img")

        cover = None

        if img:
            cover = img.get("src")

        books.append({
            "source": "novelflow",
            "title": title[:120],
            "author": "Unknown",
            "genre": genre,
            "cover": cover,
            "download": full_url,
        })

    return books


def scrape_nf_playwright(slug, genre):
    if not PLAYWRIGHT_AVAILABLE:
        return scrape_nf_static(slug, genre)

    url = f"https://www.novelflow.app/stories/{slug}"

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        page = browser.new_page()

        page.goto(url, wait_until="networkidle")

        for _ in range(40):
            page.mouse.wheel(0, 7000)
            page.wait_for_timeout(1500)

        html = page.content()

        browser.close()

    soup = BeautifulSoup(html, "html.parser")

    books = []

    seen = set()

    for a in soup.find_all("a", href=True):

        href = a.get("href")

        if not href:
            continue

        if not href.startswith("/novel/"):
            continue

        full_url = "https://www.novelflow.app" + href

        if full_url in seen:
            continue

        seen.add(full_url)

        title = normalize_text(a.get_text())

        if not title:
            title = "Unknown Title"

        img = a.find("img")

        cover = None

        if img:
            cover = img.get("src")

        books.append({
            "source": "novelflow",
            "title": title[:120],
            "author": "Unknown",
            "genre": genre,
            "cover": cover,
            "download": full_url,
        })

    return books


# =========================================================
# INGEST ROUTES
# =========================================================

@router.get("/obooko", dependencies=[Depends(_require_admin)])
@limiter.limit("5/minute")
def ingest_obooko(request: Request):

    scrape = scrape_obooko_playwright if USE_PLAYWRIGHT else scrape_obooko_static

    total = 0

    with get_db() as db:

        for url, genre in OBOOKO_CATEGORIES:

            try:
                books = scrape(url, genre)

                added = save_books(db, books)

                total += added

                print(f"[obooko] {genre}: {added}")

            except Exception as e:
                print("[obooko ERROR]", e)

            time.sleep(1)

    return {
        "status": "success",
        "source": "obooko",
        "count": total,
        "mode": "playwright" if USE_PLAYWRIGHT else "static"
    }


@router.get("/novelflow", dependencies=[Depends(_require_admin)])
@limiter.limit("5/minute")
def ingest_novelflow(request: Request):

    scrape = scrape_nf_playwright if USE_PLAYWRIGHT else scrape_nf_static

    total = 0

    with get_db() as db:

        for slug, genre in NF_GENRES:

            try:
                books = scrape(slug, genre)

                added = save_books(db, books)

                total += added

                print(f"[novelflow] {genre}: {added}")

            except Exception as e:
                print("[novelflow ERROR]", e)

            time.sleep(1)

    return {
        "status": "success",
        "source": "novelflow",
        "count": total,
        "mode": "playwright" if USE_PLAYWRIGHT else "static"
    }


@router.get("/all", dependencies=[Depends(_require_admin)])
@limiter.limit("2/minute")
def ingest_all(request: Request):

    o = ingest_obooko(request)
    n = ingest_novelflow(request)

    return {
        "status": "success",
        "obooko": o["count"],
        "novelflow": n["count"],
        "total": o["count"] + n["count"]
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
            query = query.filter(Book.genre.ilike(f"%{genre}%"))

        if source:
            query = query.filter(Book.source == source)

        books = query.limit(500).all()

    return [
        {
            "id": b.id,
            "title": b.title,
            "author": b.author,
            "genre": b.genre,
            "cover": b.cover,
            "download": b.download,
            "source": b.source,
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
            "title": b.title,
            "author": b.author,
            "genre": b.genre,
            "source": b.source,
        }
        for b in books
    ]