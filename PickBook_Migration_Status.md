# PickBook Gradual Migration Status

Date: 2026-07-09

## Goal

Gradually move PickBook from the current single `books`-centered architecture toward the unified production architecture:

- Managed Catalog for imported/licensed/public-domain/partner books
- Stories Platform for author-created content
- Shared Reader Platform for search, library, bookmarks, progress, downloads, and recommendations

## Completed So Far

### 0. Turned Author Submissions Into Real Draft Records

`POST /author/submit` now creates persistent story platform records instead of
only returning `received`.

It now:

- Creates or finds a temporary local author profile
- Creates a `stories` row in `pending_review`
- Creates a `drafts` row tied to the story and author
- Stores manuscript metadata such as filename, extension, MIME type, size, and submission time
- Stores text content for small `.txt` uploads while keeping `.docx` and `.epub` metadata-only for now

Files changed:

- `app/author_portal/routes.py`
- `app/models/book.py`
- `app/core/database.py`
- `sql/001_supabase_schema.sql`
- `alembic/versions/001_content_domains_and_reader_state.py`

### 1. Added Future Content Domain Tables

Added SQLAlchemy models for:

- `stories`
- `story_versions`
- `drafts`
- `search_index`

These prepare the app for a real author/story workflow without breaking the existing `books` table.

Files changed:

- `app/models/book.py`
- `sql/001_supabase_schema.sql`
- `alembic/versions/001_content_domains_and_reader_state.py`

### 2. Added Unified Reader State Tables

Added database-backed reader state tables:

- `reading_progress`
- `bookmarks`
- `user_library`
- `downloads`

Each table supports either a `book_id` or a `story_id`, with a CHECK constraint so one row cannot point to both.

This matches the production architecture direction while still supporting the current book/catalog flow.

### 3. Added Optional Account-Based Reader Sync

Users can still browse/read anonymously with local browser state.

When a user has an account, the app now syncs:

- Shelf
- Reading progress
- Bookmarks

Backend endpoints added:

- `GET /api/reader-state`
- `POST /api/reader-state`

Files changed:

- `app/features/routes.py`
- `app/templates/index.html`

### 4. Kept Downloads Server-Owned

Downloads are tied to subscription/quota rules, so general browser sync does not overwrite download records.

The download endpoint now records a server-side `downloads` row when a Standard user downloads a book.

The frontend merges server download metadata with local offline payloads so local offline reading data is not erased.

### 5. Kept Account Creation Optional

No forced signup on first visit.

Anonymous users can still use PickBook locally.

Account creation/login is used when needed for account-backed features such as server sync, paid plan state, and downloads.

### 6. Added Basic Author Studio Dashboard

Authors now have a first usable dashboard/upload manager inside the main PickBook UI.

It includes:

- An `Author Studio` navigation entry on desktop and mobile
- A draft upload form for `.txt`, `.docx`, and `.epub` manuscripts
- A status list showing submitted drafts and review state
- A refresh action for loading current submission status
- Browser/account session continuity for anonymous authors after their first upload

Backend endpoint added:

- `GET /author/submissions`

`POST /author/submit` now also returns:

- `author_id`
- `author_access_token`
- `story_id`
- `draft_id`
- manuscript metadata

The returned `author_access_token` lets the dashboard work in production-style auth mode instead of relying on raw local profile IDs.

Files changed:

- `app/author_portal/routes.py`
- `app/templates/index.html`

### 7. Added Visible Account And Sync Controls

Readers now have visible account/sync controls in the main navigation.

It includes:

- An `Account` button in desktop navigation
- A mobile menu account entry
- A sync status chip that shows whether reader state is local-only, syncing, or account-backed
- Account creation from the nav without forcing signup on first visit
- Reader state push/restore status updates after account creation, session refresh, and sync

Files changed:

- `app/templates/index.html`
- `start-local.ps1`

## Verification Done

Passed:

- Python compile check:
  - `python -m compileall app alembic`
- Author submission smoke test:
  - submitted a `.txt` manuscript to `POST /author/submit`
  - confirmed a local author profile was created
  - confirmed a `stories` row was created with `pending_review`
  - confirmed a `drafts` row was created with manuscript metadata and text content
- SQLAlchemy table creation smoke test:
  - confirmed all model tables create successfully
- FastAPI route test:
  - created a test account
  - pushed reader state
  - read reader state back
  - confirmed shelf/progress/bookmark data persisted
  - confirmed server-owned download row was not erased by reader-state sync
- Author dashboard smoke test:
  - submitted a `.txt` manuscript through `POST /author/submit`
  - confirmed an author access token was returned
  - called `GET /author/submissions` with the returned token
  - confirmed submitted drafts were returned with `pending_review` status
- Homepage render check:
  - confirmed `GET /` returned 200 in `TestClient`
  - confirmed the rendered HTML contains `Author Studio`
  - confirmed the rendered HTML contains `submitAuthorDraft`
- Local server check:
  - `GET /health` returned OK
  - `GET /` returned 200 and contained PickBook homepage HTML
- Account/sync UI static check:
  - confirmed the rendered HTML contains `sync-status-chip`
  - confirmed the rendered HTML contains `openAccountControls`
- Local server recheck:
  - fixed `start-local.ps1` fallback startup on this Windows/PowerShell environment
  - confirmed `http://127.0.0.1:8000/` serves HTML containing `sync-status-chip`
  - confirmed `http://127.0.0.1:8000/` serves HTML containing `openAccountControls`

Could not complete:

- In-app browser visual check, because the browser surface blocked `localhost` / `127.0.0.1` with `ERR_BLOCKED_BY_CLIENT`.

## Current Local App URL

Use:

```text
http://127.0.0.1:8000/
```

Do not use `/health` for the app UI. That endpoint only confirms the backend is alive.

## Important Notes

The current app still uses the existing `books` table for visible catalog content.

The new `stories`, `story_versions`, and `drafts` tables are now used for initial author draft submissions and dashboard status.

The author workflow is still basic: uploads create one draft per story and show review status, but there is not yet a full draft editor, version history UI, moderation queue, or publishing workflow.

The account system currently uses the existing local profile/auth flow. Full Supabase Auth/JWT integration is still a later step.

## Still To Do

### Next Best Slice

Move shelf/progress/bookmark UI fully onto server state when logged in, including clearer conflict handling between local browser data and account-backed data.

### Reader Platform

Still needed:

- Server-backed reading history
- Better progress conflict handling between devices
- Server-backed library UI for logged-in users
- Richer account profile/settings panel

### Managed Catalog

Still needed:

- Rename or conceptually wrap current `books` table as Managed Catalog
- Add metadata quality/status fields
- Add source/license tracking

### Stories Platform

Still needed:

- Draft editor or richer upload manager
- Author-side draft detail page
- Replace short-lived author dashboard token with durable Supabase Auth once production auth is connected
- Version submission workflow
- Editorial/moderation queue
- Publish/schedule flow
- Reader view for published story versions

### Search And Recommendations

Still needed:

- Populate `search_index` from books
- Later populate `search_index` from published stories
- Add PostgreSQL full-text search
- Add pgvector/embedding recommendations later, after enough content and user activity exists

### Supabase Production Work

Still needed:

- Apply `sql/001_supabase_schema.sql` or Alembic migration to Supabase
- Review Row Level Security policies for the new tables
- Replace temporary local auth behavior with Supabase Auth/JWT validation
- Ensure user-owned rows are protected by `auth.uid()`

## Recommended Migration Order

1. Make author submissions create real drafts.
2. Add a basic author dashboard/upload manager.
3. Add account/login UI controls and a clear optional sync message.
4. Move shelf/progress/bookmark UI fully onto server state when logged in.
5. Create published story reader endpoints.
6. Add moderation workflow.
7. Add unified search.
8. Add recommendations.
