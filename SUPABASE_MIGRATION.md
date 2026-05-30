# PickBook Supabase Migration

This migration moves PickBook's production database and uploaded files to Supabase:

- Postgres tables, indexes, RLS policies, and Storage buckets are created by `sql/001_supabase_schema.sql`.
- Example coupons can be seeded with `sql/002_seed_example_coupons.sql`.
- Book catalog data can be loaded from `books.compact.sql` or a production dump.
- Author covers and signed deal documents can be stored in Supabase Storage when `STORAGE_BACKEND=supabase`.

## 1. Create Supabase Schema

1. Open Supabase SQL Editor.
2. Paste and run `sql/001_supabase_schema.sql`.
3. Optional: paste and run `sql/002_seed_example_coupons.sql`.

The schema creates these Storage buckets:

- `pickbook-public`: public covers, currently `covers/*`.
- `pickbook-private`: private signed agreement uploads, currently `deal-documents/*`.

## 2. Configure App Environment

Use the Supabase transaction pooler URI for hosted deployments:

```env
APP_ENV=production
DATABASE_URL=postgresql://postgres.[PROJECT-REF]:[DB-PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres?sslmode=require
DATABASE_SSLMODE=require
LOCAL_DATABASE_FALLBACK=false

STORAGE_BACKEND=supabase
SUPABASE_URL=https://[PROJECT-REF].supabase.co
SUPABASE_SERVICE_ROLE_KEY=[SERVICE_ROLE_KEY]
SUPABASE_PUBLIC_BUCKET=pickbook-public
SUPABASE_PRIVATE_BUCKET=pickbook-private
```

Keep `SUPABASE_SERVICE_ROLE_KEY` only on the server. It must never be shipped to browser code.

## 3. Load Book Data

For the bundled compact seed, start the app with the Supabase `DATABASE_URL`, then call:

```bash
curl -X POST https://your-domain.com/admin/seed/compact-books \
  -H "x-admin-token: $ADMIN_TOKEN"
```

For a Postgres dump, export data from the source and restore into Supabase:

```bash
pg_dump "$SOURCE_DATABASE_URL" --data-only --table=books > books.sql
psql "$SUPABASE_DATABASE_URL" < books.sql
```

After loading explicit book IDs, reset the sequence:

```sql
select setval(
    'books_id_seq'::regclass,
    greatest((select coalesce(max(id), 1) from public.books), 1),
    true
);
```

Validate:

```sql
select count(*) from public.books;
select source, count(*) from public.books group by source order by source;
```

## 4. Payment And Email Env

```env
PAYSTACK_SECRET_KEY=sk_test_or_live_xxxxx
PAYSTACK_PUBLIC_KEY=pk_test_or_live_xxxxx
PAYSTACK_CALLBACK_URL=https://your-domain.com/payment-success
STANDARD_PLAN_PRICE_KOBO=250000

SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM_EMAIL=
SMTP_USE_TLS=true
```

If `PAYSTACK_SECRET_KEY` is empty, PickBook returns a local mock checkout URL for development.

## 5. Local Development

For local SQLite or local Postgres development, leave storage local:

```env
APP_ENV=development
STORAGE_BACKEND=local
LOCAL_DATABASE_FALLBACK=true
```

Local uploads continue to use:

- `public/uploads/covers`
- `private_uploads/deal_documents`
