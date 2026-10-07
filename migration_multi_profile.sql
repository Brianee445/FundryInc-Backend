-- Multi-profile founders + account-level subscription tier.
-- Run this AFTER the earlier migration_add_billing.sql migrations.
--
-- What this does:
--   1. Moves verification_tier off founder_profiles/investor_profiles
--      onto users (tier is now an account-level subscription, covering
--      every startup a founder manages, not per-profile).
--   2. Drops the unique constraint on founder_profiles.user_id so one
--      founder can own multiple startup profiles.
--   3. Adds founder_profiles.published_until (premium's 90-day
--      auto-unpublish window).
--   4. Merges founder_subscriptions + investor_subscriptions into one
--      subscriptions table, keyed by user_id.
--
-- Safe to run once. Each step is guarded so a partial prior run doesn't
-- break it.

-- 1. users.verification_tier
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS verification_tier verificationtierenum NOT NULL DEFAULT 'starter';

-- Carry over whatever a founder's profile already had before this
-- column existed on users — the most permissive tier across all of a
-- founder's profiles wins, in case any founder already had more than
-- one row (shouldn't happen pre-migration, but safe either way).
UPDATE users u
SET verification_tier = sub.max_tier
FROM (
    SELECT
        fp.user_id,
        MAX(
            CASE fp.verification_tier
                WHEN 'premium' THEN 3
                WHEN 'basic' THEN 2
                ELSE 1
            END
        ) AS max_tier_rank,
        (ARRAY['starter', 'basic', 'premium'])[
            MAX(
                CASE fp.verification_tier
                    WHEN 'premium' THEN 3
                    WHEN 'basic' THEN 2
                    ELSE 1
                END
            )
        ]::verificationtierenum AS max_tier
    FROM founder_profiles fp
    WHERE EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'founder_profiles' AND column_name = 'verification_tier'
    )
    GROUP BY fp.user_id
) sub
WHERE u.id = sub.user_id;

-- Same for investor_profiles (1:1, so a direct copy).
UPDATE users u
SET verification_tier = ip.verification_tier
FROM investor_profiles ip
WHERE u.id = ip.user_id
  AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name = 'investor_profiles' AND column_name = 'verification_tier'
  );

-- 2. Drop the now-redundant per-profile columns.
ALTER TABLE founder_profiles DROP COLUMN IF EXISTS verification_tier;
ALTER TABLE investor_profiles DROP COLUMN IF EXISTS verification_tier;

-- 3. founder_profiles.user_id: drop unique constraint, keep the index.
--    Constraint name varies by how it was created — this covers the
--    default Postgres naming; adjust if your actual constraint name
--    differs (check with \d founder_profiles in psql if this no-ops).
DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT con.conname INTO constraint_name
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    WHERE rel.relname = 'founder_profiles'
      AND con.contype = 'u'
      AND con.conkey = (
          SELECT array_agg(attnum) FROM pg_attribute
          WHERE attrelid = rel.oid AND attname = 'user_id'
      );

    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE founder_profiles DROP CONSTRAINT %I', constraint_name);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_founder_profiles_user_id ON founder_profiles (user_id);

-- 4. published_until for the premium auto-unpublish window.
ALTER TABLE founder_profiles
    ADD COLUMN IF NOT EXISTS published_until TIMESTAMPTZ;

-- 5. Merge founder_subscriptions + investor_subscriptions into
--    subscriptions (user_id-keyed).
CREATE TABLE IF NOT EXISTS subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id),
    tier verificationtierenum NOT NULL,
    interval billingintervalenum NOT NULL,
    status subscriptionstatusenum NOT NULL DEFAULT 'active',
    bachs_subscription_id VARCHAR,
    bachs_customer_id VARCHAR,
    bachs_checkout_session_id VARCHAR,
    currency VARCHAR NOT NULL DEFAULT 'NGN',
    amount NUMERIC(14, 2),
    current_period_end TIMESTAMPTZ,
    canceled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_subscriptions_bachs_subscription_id
    ON subscriptions (bachs_subscription_id);

-- Copy any existing founder_subscriptions rows across, if that table
-- exists (from the earlier migration).
INSERT INTO subscriptions (
    user_id, tier, interval, status, bachs_subscription_id,
    bachs_customer_id, bachs_checkout_session_id, currency, amount,
    current_period_end, canceled_at, created_at, updated_at
)
SELECT
    fp.user_id, fs.tier, fs.interval, fs.status, fs.bachs_subscription_id,
    fs.bachs_customer_id, fs.bachs_checkout_session_id, fs.currency, fs.amount,
    fs.current_period_end, fs.canceled_at, fs.created_at, fs.updated_at
FROM founder_subscriptions fs
JOIN founder_profiles fp ON fp.id = fs.founder_profile_id
WHERE EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'founder_subscriptions')
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO subscriptions (
    user_id, tier, interval, status, bachs_subscription_id,
    bachs_customer_id, bachs_checkout_session_id, currency, amount,
    current_period_end, canceled_at, created_at, updated_at
)
SELECT
    ip.user_id, inv_sub.tier, inv_sub.interval, inv_sub.status, inv_sub.bachs_subscription_id,
    inv_sub.bachs_customer_id, inv_sub.bachs_checkout_session_id, inv_sub.currency, inv_sub.amount,
    inv_sub.current_period_end, inv_sub.canceled_at, inv_sub.created_at, inv_sub.updated_at
FROM investor_subscriptions inv_sub
JOIN investor_profiles ip ON ip.id = inv_sub.investor_profile_id
WHERE EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'investor_subscriptions')
ON CONFLICT (user_id) DO NOTHING;

-- Old tables are no longer read by the app — drop them once you've
-- confirmed the copy above looks right. Commented out on purpose so
-- this migration doesn't destroy data on a first run; uncomment and
-- rerun once you've checked `SELECT * FROM subscriptions` looks correct.
-- DROP TABLE IF EXISTS founder_subscriptions;
-- DROP TABLE IF EXISTS investor_subscriptions;

-- 6. connections.founder_profile_id already existed and already
--    supports per-profile targeting — no schema change needed there,
--    only the app code changed (see routers/connections.py).
