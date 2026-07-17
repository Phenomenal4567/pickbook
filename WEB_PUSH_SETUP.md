# PickBook Web Push Setup

This guide explains how to use the Web Push files added for PickBook.

## Files Added

- `sql/003_push_subscriptions.sql`
  Supabase migration for storing browser push subscriptions.

- `public/service-worker.js`
  Existing PWA service worker extended with `push` and `notificationclick` handlers.

- `public/push-subscription-manager.js`
  Frontend helper for requesting permission, subscribing the browser, and sending the subscription to the backend.

- `push-api/`
  Standalone Node.js/Express API for Railway. It uses Supabase and the `web-push` npm package.

## 1. Run the Supabase Migration

Open Supabase SQL Editor and run:

```sql
-- Use the contents of:
-- sql/003_push_subscriptions.sql
```

This creates `public.push_subscriptions` linked to `auth.users(id)` with cascade delete.

## 2. Generate VAPID Keys

From the `push-api` folder:

```bash
npm install
npx web-push generate-vapid-keys
```

Copy the public and private keys into Railway environment variables.

## 3. Configure Railway Environment

Set these variables for the `push-api` Railway service:

```env
PORT=8080
NODE_ENV=production
APP_ORIGIN=https://your-pickbook-domain.com

SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key

VAPID_PUBLIC_KEY=your-vapid-public-key
VAPID_PRIVATE_KEY=your-vapid-private-key
VAPID_SUBJECT=mailto:admin@your-domain.com

PUSH_ADMIN_TOKEN=a-long-random-secret
```

`SUPABASE_KEY` is also accepted as an alias for `SUPABASE_SERVICE_ROLE_KEY`.

## 4. Deploy the Node Push API

Deploy the `push-api` folder as its own Railway service.

Start command:

```bash
npm start
```

Health check:

```text
GET /health
```

Expected response:

```json
{ "ok": true, "service": "pickbook-push-api" }
```

## 5. Connect the Frontend

Import and call the subscription helper after the user signs in.

```js
import { subscribePickBookUserToPush } from "/push-subscription-manager.js";

await subscribePickBookUserToPush({
  userId: state.userId,
  accessToken: state.accessToken,
  vapidPublicKeyEndpoint: "https://your-push-api.up.railway.app/api/push/vapid-public-key",
  subscribeEndpoint: "https://your-push-api.up.railway.app/api/push/subscribe",
});
```

The helper:

- checks browser support
- requests notification permission
- registers `/service-worker.js`
- subscribes with the VAPID public key
- posts the browser subscription to the Railway backend

## 6. Send a Notification

Call the protected backend endpoint:

```bash
curl -X POST https://your-push-api.up.railway.app/api/push/send \
  -H "Content-Type: application/json" \
  -H "X-Push-Admin-Token: your-push-admin-token" \
  -d '{
    "targetUserId": "supabase-user-id",
    "title": "New chapter is live",
    "body": "Continue reading Shadows of Destiny.",
    "url": "/?book=123&chapter=8"
  }'
```

To target every stored subscription:

```json
{
  "segment": "all",
  "title": "PickBook update",
  "body": "New stories are ready for you.",
  "url": "/"
}
```

## 7. Notification Click Behavior

When a reader taps a notification, the service worker:

- closes the notification
- focuses an existing PickBook tab if one is open
- navigates it to the notification `url`
- opens a new PickBook window if no tab is open

Only same-origin URLs are allowed by the service worker redirect safety check.

## 8. Stale Subscription Cleanup

If a browser push endpoint returns `404 Not Found` or `410 Gone`, the backend automatically deletes that subscription from Supabase.

This handles revoked permissions, expired browser sessions, and deleted push endpoints.

## Quick Test Checklist

1. Run the Supabase migration.
2. Deploy `push-api` to Railway.
3. Confirm `/health` works.
4. Confirm `/api/push/vapid-public-key` returns a public key.
5. Sign in to PickBook.
6. Call `subscribePickBookUserToPush(...)`.
7. Confirm a row appears in `push_subscriptions`.
8. Send a test push with `/api/push/send`.
9. Tap the notification and confirm it opens the target PickBook path.
