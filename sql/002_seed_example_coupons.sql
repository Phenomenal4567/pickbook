-- Optional PickBook coupon examples.
-- Edit or remove these before production if you do not want default promos.

insert into public.coupons (
    code,
    discount_type,
    discount_value,
    plan_target,
    max_uses,
    expiry_date
)
values
    ('WELCOME30', 'free_days', 30, 'standard', 500, null),
    ('EASTER50', 'percent', 50, 'standard', 200, null),
    ('FLAT500', 'fixed_amount', 500, 'standard', 100, null),
    ('VIPFREE7', 'free_days', 7, 'standard', 50, now() + interval '48 hours')
on conflict (code) do nothing;
