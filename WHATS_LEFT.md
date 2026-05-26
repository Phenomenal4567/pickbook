# PickBook - What Is Left To Do

## Done

- Removed old `novelflow` and `obooko` records from the database.
- Kept existing `anystories` records.
- Added `alphanovel` ingestion.
- Fixed AlphaNovel titles so badge text like `Exclusive Updated` is not saved as the title.
- Fixed AlphaNovel cover extraction.
- Added database fields for:
  - `chapters_count`
  - `chapter_content`
- Added a chapter API:
  - `/ingest/books/{book_id}/chapters/{chapter_number}`
- Updated the frontend reader to load chapter content from the backend instead of fake placeholder text.
- Refreshed AlphaNovel rows so all 112 AlphaNovel books now have covers and cached chapter content.

## Still Left

### 1. Re-ingest Anystories With Covers And Content

AlphaNovel now has covers and cached chapters. Anystories still needs the same deeper detail-page scraping.

Needed:
- Visit each Anystories book detail page after scraping the listing page.
- Extract the real cover URL from the detail page if the listing page does not provide it.
- Extract public chapter content if Anystories exposes it.
- Save `cover`, `chapters_count`, and `chapter_content`.

### 2. Support More Than The First Public Chapters

AlphaNovel exposes some public chapter content on the detail page, usually the first two chapters. Later chapters may require their reader page, app access, login, or coins.

Needed:
- Decide whether PickBook should cache only public preview chapters.
- For unavailable chapters, keep showing the source link instead of fake text.
- Optional: add a background job that tries to fetch more public chapters where allowed.

### 3. Rebuild A Clean Final Zip Again

After these latest fixes, make a new clean zip excluding:
- `.env`
- `.git`
- `__pycache__`
- `.pyc`
- debug HTML files

Suggested final zip name:

```text
Pickbook_clean_final.zip
```

### 4. Add Admin Buttons Or Commands

Right now ingestion is done by opening API URLs or running PowerShell commands.

Useful admin actions to add:
- `Ingest Anystories`
- `Ingest AlphaNovel`
- `Clear Source`
- `Refresh Covers`
- `Refresh Chapters`

### 5. Improve The Reader UX

Current reader now loads real cached chapter content where available.

Nice improvements:
- Show `Cached chapter available` or `Open source story` clearly.
- Hide unavailable chapter numbers if only a few chapters are cached.
- Add a direct `Continue on source` button in the detail page.

### 6. Check Content Rights

Before making the app public, confirm what each source allows.

Recommended safe approach:
- Store metadata, covers, synopsis, and links.
- Cache only public preview chapters where clearly available.
- For full chapters, send users to the original source.

### 7. Production Cleanup

Before deployment:
- Rotate any exposed secrets in `.env`.
- Confirm `DATABASE_URL` points to the correct production database.
- Set a strong `ADMIN_TOKEN`.
- Set `APP_ENV=production`.
- Confirm `ALLOWED_ORIGINS` matches the live domain.
- Run the app once and check:
  - `/health`
  - `/`
  - `/ingest/books`
  - `/ingest/books?source=alphanovel`

## Useful URLs

Local app:

```text
http://127.0.0.1:8000
```

All books:

```text
http://127.0.0.1:8000/ingest/books
```

AlphaNovel books:

```text
http://127.0.0.1:8000/ingest/books?source=alphanovel
```

Anystories books:

```text
http://127.0.0.1:8000/ingest/books?source=anystories
```

## Current Best Next Step

Fix Anystories detail scraping next, so it gets covers and readable preview chapters like AlphaNovel now does.
