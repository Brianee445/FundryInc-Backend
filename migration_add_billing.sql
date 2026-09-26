-- Bachs billing v2: 3-tier scheme (starter/basic/premium) for BOTH
-- founders and investors. Run this AFTER migration_add_billing.sql (v1)
-- if you already applied that one — this migrates its objects forward
-- rather than recreating them, and is safe to run once on a v1 database.
--
-- If you have NOT run the v1 migration yet, skip it and run this file
-- alone; the CREATE TABLE IF NOT EXISTS / guarded ALTERs below build the
-- full v2 shape from a clean slate too.

-- 1. verification_tier: basic/business_verified/investor_ready -> starter/basic/premium
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'verificationtierenum') THEN
        ALTER TYPE verificationtierenum RENAME VALUE 'basic' TO 'starter';
        ALTER TYPE verificationtierenum RENAME VALUE 'business_verified' TO 'basic';
        ALTER TYPE verificationtierenum RENAME VALUE 'investor_ready' TO 'premium';
    ELSE
        CREATE TYPE verificationtierenum AS ENUM ('starter', 'basic', 'premium');
    END IF;
END $$;

-- founder_profiles.verification_tier already existed under the old enum
-- values; the rename above updates it in place. Just fix the default.
ALTER TABLE founder_profiles
    ALTER COLUMN verification_tier SET DEFAULT 'starter';

-- investor_profiles never had this column before now.
ALTER TABLE investor_profiles
    ADD COLUMN IF NOT EXISTS verification_tier verificationtierenum NOT NULL DEFAULT 'starter';

-- 2. founder_subscriptions: plan (monthly/annual) -> tier + interval
DO $$ BEGIN
    CREATE TYPE billingintervalenum AS ENUM ('monthly', 'annual');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE subscriptionstatusenum AS ENUM ('active', 'past_due', 'canceled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'founder_subscriptions'
    ) THEN
        -- v1 table exists: rename plan -> interval, add tier.
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'founder_subscriptions' AND column_name = 'plan'
        ) THEN
            ALTER TABLE founder_subscriptions RENAME COLUMN plan TO interval_old;
            ALTER TABLE founder_subscriptions
                ADD COLUMN interval billingintervalenum;
            UPDATE founder_subscriptions SET interval = interval_old::text::billingintervalenum;
            ALTER TABLE founder_subscriptions ALTER COLUMN interval SET NOT NULL;
            ALTER TABLE founder_subscriptions DROP COLUMN interval_old;

            ALTER TABLE founder_subscriptions
                ADD COLUMN tier verificationtierenum NOT NULL DEFAULT 'basic';
        END IF;
    ELSE
        CREATE TABLE founder_subscriptions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            founder_profile_id UUID NOT NULL UNIQUE REFERENCES founder_profiles(id),
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
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_founder_subscriptions_bachs_subscription_id
    ON founder_subscriptions (bachs_subscription_id);

-- 3. investor_subscriptions — new table, mirrors founder_subscriptions.
CREATE TABLE IF NOT EXISTS investor_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_profile_id UUID NOT NULL UNIQUE REFERENCES investor_profiles(id),
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

CREATE INDEX IF NOT EXISTS ix_investor_subscriptions_bachs_subscription_id
    ON investor_subscriptions (bachs_subscription_id);

-- 4. billing_events — unchanged from v1, created here too in case v1 was
-- never applied.
CREATE TABLE IF NOT EXISTS billing_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bachs_event_id VARCHAR NOT NULL UNIQUE,
    event_type VARCHAR NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_billing_events_bachs_event_id
    ON billing_events (bachs_event_id);
