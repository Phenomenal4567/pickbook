# PickBook Push API

Railway-ready Express API for Web Push notifications using Supabase and VAPID.

## Railway start command

```bash
npm install
npm start
```

## Required environment variables

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `VAPID_PUBLIC_KEY`
- `VAPID_PRIVATE_KEY`
- `VAPID_SUBJECT`
- `PUSH_ADMIN_TOKEN`
- `APP_ORIGIN`
- `PORT` (Railway usually injects this)

Generate VAPID keys:

```bash
npx web-push generate-vapid-keys
```
