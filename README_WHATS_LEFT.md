# PickBook Handoff: What Is Left

## Current State

- The app should be opened through the FastAPI server, not by opening `app/templates/index.html` directly.
- Use this local URL while developing:

```text
http://127.0.0.1:8000/
```

- Opening the template as `file://.../index.html` will not load stories because the frontend calls `/ingest/books`, which only exists when the backend is running.
- The local API was checked and returned books from:

```text
http://127.0.0.1:8000/ingest/books
```

## Recent Changes Made

- Updated source genre labels in ingestion:
  - Anystories `werewolf` now maps to `Werewolf`.
  - Anystories `dark-romance` maps to `Dark Romance`.
  - MoboReader `werewolf` now maps to `Werewolf`.
  - MoboReader `billionaire` now maps to `Billionaire`.
  - MoboReader `ya-teen` now maps to `YA/Teen`.
- Updated the frontend genre filters and taste picker to include:
  - `Werewolf`
  - `Billionaire`
  - `Dark Romance`
  - `YA/Teen`
- Added an Anystories detail-enrichment path in `app/ingest/routes.py` for:
  - detail-page cover extraction
  - synopsis extraction
  - chapter count extraction
  - public preview chapter caching where available
- Fixed the Anystories listing parser so it uses each book link's own title/image metadata before falling back to broader page headings. This prevents repeated bad titles like `List of Dark Romance Novels to Read Online`.
- Updated save logic so re-ingest can repair existing bad Anystories rows that already have those listing-page titles.
- Added source filters in the frontend for `Anystories` and `MoboReader`, so Anystories books can be found directly.
- Updated the reader/chapter list so chapters cached in PickBook are marked `Cached`, while unavailable chapters show `Continue on source` instead of pretending they are readable locally.
- Ran `scripts/repair_anystories_rows.py` to repair 79 existing bad Anystories titles, then reran it to polish possessive title formatting.
- Added a file-open redirect guard in `index.html` so direct `file://` opens try to move to the local server.

## Important Issue Still To Fix

Some existing Anystories rows in the database were previously scraped incorrectly. They may still show titles like:

```text
List of Werewolf Novels to Read Online
List of Dark Romance Novels to Read Online
```

The parser has been fixed, and the existing bad titles were repaired from their story URLs. Full detail enrichment still needs a slower Anystories re-ingest when time allows.

## Best Next Steps

1. Start the backend locally.

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

2. Open the app at:

```text
http://127.0.0.1:8000/
```

3. Confirm stories render on the home page.

4. Clear bad Anystories rows, then re-ingest Anystories.

Useful endpoint:

```text
http://127.0.0.1:8000/ingest/anystories
```

Requires the `X-Admin-Token` header.

5. Verify Anystories records now have real:

- title
- author where available
- cover
- synopsis
- chapters count
- cached public preview chapters where available

6. Re-check genre filters in the UI:

- `Romance`
- `Fantasy`
- `Werewolf`
- `Dark Romance`
- `Billionaire`
- `Paranormal`
- `YA/Teen`
- `LGBTQ+`

## Cleanup Before Final Zip

Create a clean zip excluding:

- `.env`
- `.git`
- `__pycache__`
- `.pyc`
- debug HTML files
- local server logs

Suggested name:

```text
Pickbook_clean_final.zip
```

## Production Safety

Before deployment:

- Rotate any secrets exposed in `.env`.
- Confirm `DATABASE_URL` points to the intended production database.
- Set a strong `ADMIN_TOKEN`.
- Confirm `ALLOWED_ORIGINS` matches the deployed frontend domain.
- Run:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/ingest/books
```
