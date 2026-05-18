# PickBook Phase 5 — Clean Final Release

Production-ready launch backend for PickBook.

## Features

- Author submission portal with file-type + size validation
- Admin moderation dashboard (token-protected)
- License validation
- Offline PWA support with stale-while-revalidate caching
- Plausible analytics integration
- Dockerized PostgreSQL + Redis with persistent volumes & health checks
- Health monitoring endpoints
- Rate limiting on all API routes
- Input sanitisation (bleach) on all free-text fields

## Quick Start

### 1. Configure environment
```bash
cp .env.example .env
# Edit .env — set SECRET_KEY, POSTGRES_PASSWORD, ADMIN_TOKEN, ALLOWED_ORIGINS
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Start infrastructure + server
```bash
./start.sh
```
The script waits for Postgres and Redis health checks before starting uvicorn.

## API Endpoints

| Method | Path | Auth | Rate limit |
|--------|------|------|------------|
| GET | `/` | none | — |
| GET | `/health` | none | — |
| POST | `/author/submit` | none | 10 req/min |
| GET | `/admin/dashboard` | `X-Admin-Token` header | 60 req/min |

## Security Notes

- **CORS**: `allow_origins` is driven by `ALLOWED_ORIGINS` env var — never `"*"` with credentials.
- **Admin routes**: protected by `X-Admin-Token` header. Swap `app/core/auth.py` for Clerk/Supabase JWT in production SSO setups.
- **File uploads**: extension allowlist + 50 MB size cap enforced before reading content.
- **Input sanitisation**: all free-text form fields are run through `bleach.clean()`.
- **Rate limiting**: `slowapi` applied per-route.
- **Secrets**: all credentials in `.env` — never hardcoded.

## Deployment

### Railway
Reads `app/deployment/railway.json` and `$PORT` automatically.

### Vercel
```bash
vercel --prod
```
Routes are configured in `app/deployment/vercel.json`.
# pickbook
