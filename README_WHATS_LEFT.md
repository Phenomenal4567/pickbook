# PickBook Handoff: Author-Only Catalog

## Current State

- PickBook should be opened through the FastAPI server, not by opening `app/templates/index.html` directly.
- Use this local URL while developing:

```text
http://127.0.0.1:8000/
```

- Reader-facing catalog data now comes from published author submissions only.
- The frontend loads stories from:

```text
http://127.0.0.1:8000/api/books
```

## Author Story Flow

1. Authors apply for author access.
2. Admins approve author applications.
3. Approved authors submit stories through the author portal.
4. Admins review submitted stories.
5. Approved stories are published into the reader catalog as PickBook originals.

## Useful Local Checks

Start the backend locally:

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/api/books
```

## Best Next Steps

1. Confirm the home page renders only author-published stories.
2. Use `/admin/tools` to review author applications and submitted stories.
3. Add richer author-side draft editing and version submission.
4. Add better reader empty states for a fresh database with no published author stories yet.

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
Pickbook_author_only.zip
```

## Production Safety

Before deployment:

- Rotate any secrets exposed in `.env`.
- Confirm `DATABASE_URL` points to the intended production database.
- Set a strong `ADMIN_TOKEN`.
- Confirm `ALLOWED_ORIGINS` matches the deployed frontend domain.
