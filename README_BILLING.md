# Billing — multi-profile founders, account-level tiers, Paystack

Tiers (account-level — `User.verification_tier`, covers every startup a
founder manages, not per-profile):

| Tier | Price | Startup profiles | Intro messages/30d | Other |
|---|---|---|---|---|
| **starter** (free) | — | 1 | — (can't send intros) | — |
| **basic** (orange) | ₦2,000/mo · ₦24,000/yr | up to 3 | 5 | — |
| **premium** (gold) | ₦5,000/mo · ₦60,000/yr (₦50,000/yr promo) | unlimited | unlimited | blue-tick badge, each profile auto-unpublishes 90 days after (re)publishing, suggested to investors |

Investors have the same three tiers/prices; the limits above (profile
count, intro cap, auto-unpublish) only apply to founders.

## Billing provider: Paystack (switched from Bachs)

- **No sandbox base URL.** Test vs live is just the API key prefix —
  `sk_test_...` or `sk_live_...` — against the same
  `https://api.paystack.co`. See `app/services/paystack.py`.
- **Plans, not "products."** Paystack calls the recurring price object a
  *plan* (`PLN_...`), created in the Paystack dashboard. A subscription
  is created by attaching a plan code to `POST /transaction/initialize`
  — there's no separate "create subscription" call for this flow.
- **Amounts are in kobo** (smallest unit), not a decimal string. See
  `paystack.to_kobo()`.
- **Webhook signature**: HMAC-**SHA512** over the raw body, header
  `x-paystack-signature`, keyed with the same secret key used for API
  calls — Paystack has no separate webhook-signing secret the way Bachs
  did. See `app/services/webhook_verify.py`.
- **Webhook events**: `charge.success` (first payment and every
  renewal), `subscription.create` (carries `subscription_code` +
  `email_token`, both required to cancel later), `subscription.disable`
  (cancellation confirmed), `invoice.payment_failed`.
- **Cancelling** requires both `subscription_code` and `email_token` —
  Paystack hands both back on `subscription.create`, never something we
  generate ourselves. See `Subscription.paystack_subscription_code` /
  `paystack_email_token` in `models.py`.

## Setup
1. Create **8** recurring plans in the Paystack dashboard at the prices
   above (basic/premium × monthly/annual × founder/investor). Paste all
   8 `PLN_...` codes into env vars — see `.env.example`.
2. Set `PAYSTACK_SECRET_KEY` (`sk_test_...` to start) and
   `PAYSTACK_PUBLIC_KEY` in Render.
3. Point a Paystack webhook (in the dashboard's API Keys & Webhooks
   settings) at `POST /api/v1/billing/webhook`. No separate secret to
   configure there — Paystack signs with your existing secret key.
4. Run `migration_switch_to_paystack.sql` (after
   `migration_multi_profile.sql` has already been applied). It renames
   the Bachs-named columns on `subscriptions`/`billing_events` to their
   Paystack equivalents and adds the new `paystack_email_token` column.
5. Test in Paystack's test mode end to end before swapping to
   `sk_live_...`.

## Multi-profile founders (unchanged from the earlier migration)
- `User.verification_tier` — tier lives on the account, not any one
  startup profile. A founder's plan covers every startup they own.
- `FounderProfile.user_id` is no longer unique — founders can own
  multiple startup profiles, capped by `PROFILE_LIMITS` in
  `routers/founder_profiles.py` (1/3/unlimited).
- `FounderProfile.published_until` — premium's 90-day auto-unpublish
  window; checked lazily on read (`_maybe_auto_unpublish`), not via a
  background job.
- `GET/POST /founder-profiles/mine`, `PUT`/`DELETE /{profile_id}` manage
  individual profiles; the old singular `/me` endpoints still work
  against the founder's oldest profile for anything not yet updated.
- `connections.py`: founder-initiated requests (`POST /to-investor`)
  require `founder_profile_id` and are capped at 5/30 days for `basic`
  founders (unlimited on `premium`, blocked outright on `starter`),
  counted across all their profiles. `/sent` and `/received` aggregate
  across every profile a founder owns.
- `analytics.py`: founder analytics aggregate across all of a founder's
  profiles.

## Notes
- The frontend's `/billing` page and dashboard upgrade flow needed no
  changes for the Paystack switch — `CheckoutResponse.checkout_url` is
  populated with Paystack's `authorization_url` on our side, so the
  contract the frontend relies on is unchanged.
