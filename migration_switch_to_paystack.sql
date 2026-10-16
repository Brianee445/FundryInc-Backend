-- Switches billing from Bachs to Paystack. Run this AFTER
-- migration_multi_profile.sql has already been applied (the
-- `subscriptions` and `billing_events` tables must already exist).
--
-- Renames columns in place rather than dropping/recreating, so any
-- existing subscription rows (from testing with Bachs) keep their
-- row-level data intact, even though the Bachs-side ids they held are
-- now meaningless. If you have no real paying customers yet (likely,
-- given this was mid-testing), it's simplest to just let old
-- bachs_subscription_id/bachs_customer_id values sit unused in their
-- renamed columns and start fresh with Paystack.

ALTER TABLE subscriptions RENAME COLUMN bachs_subscription_id TO paystack_subscription_code;
ALTER TABLE subscriptions RENAME COLUMN bachs_customer_id TO paystack_customer_code;
ALTER TABLE subscriptions RENAME COLUMN bachs_checkout_session_id TO paystack_reference;

-- New column Bachs never needed — required by Paystack's
-- subscription-disable endpoint alongside the subscription code.
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS paystack_email_token VARCHAR;

-- billing_events.bachs_event_id -> provider_event_id (now provider-agnostic,
-- holds whatever id routers/billing.py's paystack_webhook derives per
-- event type).
ALTER TABLE billing_events RENAME COLUMN bachs_event_id TO provider_event_id;
