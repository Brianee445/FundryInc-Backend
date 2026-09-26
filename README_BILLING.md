# Bachs billing v2 — 3-tier, both roles

Tiers: **starter** (free, teal) → **basic** (₦3,000/mo · ₦30,000/yr, orange)
→ **premium** (₦5,000/mo · ₦50,000/yr, gold). Annual = 10x monthly (2
months free) on every tier. Both founders AND investors can subscribe —
each role has its own subscription table but the same tier scheme.

## What changed from v1
- `VerificationTierEnum`: `basic/business_verified/investor_ready` →
  `starter/basic/premium`
- `InvestorProfile` gained its own `verification_tier` column (didn't
  exist before)
- `founder_subscriptions.plan` (monthly/annual only) → split into `tier`
  (basic/premium) + `interval` (monthly/annual)
- New `investor_subscriptions` table, mirrors `founder_subscriptions`
- `/api/v1/billing/*` endpoints are now role-aware (work for both
  founders and investors, `get_current_user` instead of
  `require_role("founder")`)
- `CheckoutRequest` body is now `{tier, interval}` instead of `{plan}`

## Setup
1. Create **8** recurring products in the Bachs dashboard: basic/premium
   × monthly/annual × founder/investor. Paste all 8 `prod_...` ids into
   env vars (see `.env.example` — `BACHS_FOUNDER_BASIC_MONTHLY_PRODUCT_ID`
   etc.)
2. Run `migration_add_billing.sql` — it migrates a v1 database forward
   (renames enum values, splits `plan` into `tier`+`interval`, adds the
   investor table) OR builds the full v2 schema from scratch if v1 was
   never applied. Safe to run once either way.
3. Set the same 8 product-id vars (plus `BACHS_API_KEY`,
   `BACHS_WEBHOOK_SECRET`) in Render's dashboard — `render.yaml` only
   declares the keys, Render doesn't fill in values for you.

## Notes
- `starter` never has a subscription row — it's the implicit free
  default, not billed.
- Webhook metadata now carries `role` (`founder`/`investor`) alongside
  `profile_id`, `tier`, `interval` so one webhook handler can route to
  either subscription table.
