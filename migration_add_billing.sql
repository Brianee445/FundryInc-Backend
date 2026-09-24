-- Adds founder premium billing (Bachs). Run after the existing migrations.
-- Base.metadata.create_all() on startup will also create these if they
-- don't exist yet, but keeping an explicit migration matches this repo's
-- existing convention (see the other migration_*.sql files).

CREATE TYPE subscriptionplanenum AS ENUM ('monthly', 'annual');
CREATE TYPE subscriptionstatusenum AS ENUM ('active', 'past_due', 'canceled');

CREATE TABLE IF NOT EXISTS founder_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    founder_profile_id UUID NOT NULL UNIQUE REFERENCES founder_profiles(id),
    plan subscriptionplanenum NOT NULL,
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

CREATE INDEX IF NOT EXISTS ix_founder_subscriptions_bachs_subscription_id
    ON founder_subscriptions (bachs_subscription_id);

CREATE TABLE IF NOT EXISTS billing_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bachs_event_id VARCHAR NOT NULL UNIQUE,
    event_type VARCHAR NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_billing_events_bachs_event_id
    ON billing_events (bachs_event_id);
