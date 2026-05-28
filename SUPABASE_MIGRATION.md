# PickBook Supabase Migration

## What Is Included

- `sql/001_supabase_schema.sql` creates the PickBook tables, indexes, and RLS policies.
- `sql/002_seed_example_coupons.sql` optionally creates example promo coupons.
- The app reads `DATABASE_URL` from the environment and uses SSL for Postgres by default.

## Upload SQL To Supabase

1. Open Supabase.
2. Go to SQL Editor.
3. Paste and run `sql/001_supabase_schema.sql`.
4. Optional: paste and run `sql/002_seed_example_coupons.sql`.
5. Copy the Supabase transaction pooler URI.
6. Set Railway/Koyeb `DATABASE_URL` to that Supabase URI.

## Data Migration From Railway

Dump Railway:

```bash
pg_dump "postgresql+psycopg://postgres:ifqXjxPwEZZLzkLnspGXRRoumxGIglEy@metro.proxy.rlwy.net:21141/railway
" --data-only --table=books > books.sql
```

Restore to Supabase:

```bash
psql "postgresql://postgres.blpmvrffxbzcikhxfqhn:[YOUR-PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:6543/postgres?pgbouncer=true" < books.sql
```

Validate counts:

```sql
select count(*) from public.books;
select source, count(*) from public.books group by source order by source;
```

## Paystack Env

Set these on the server before real payments:

```env
PAYSTACK_SECRET_KEY=sk_test_or_live_xxxxx
PAYSTACK_PUBLIC_KEY=pk_test_or_live_xxxxx
PAYSTACK_CALLBACK_URL=https://your-domain.com/payment-success
STANDARD_PLAN_PRICE_KOBO=250000
```

If `PAYSTACK_SECRET_KEY` is empty, PickBook returns a local mock checkout URL for development.
