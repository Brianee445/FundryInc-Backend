# Bachs billing v3 — multi-profile founders, account-level tiers

Tiers (account-level — `User.verification_tier`, covers every startup a
founder manages, not per-profile):

| Tier | Price | Startup profiles | Intro messages/30d | Other |
|---|---|---|---|---|
| **starter** (free) | — | 1 | — (can't send intros) | — |
| **basic** (orange) | ₦2,000/mo · ₦24,000/yr | up to 3 | 5 | — |
| **premium** (gold) | ₦5,000/mo · ₦60,000/yr (₦50,000/yr promo) | unlimited | unlimited | blue-tick badge, each profile auto-unpublishes 90 days after (re)publishing, suggested to investors |

Investors have the same three tiers/prices; the limits above (profile
count, intro cap, auto-unpublish) only apply to founders.

## What changed from v2
- `User.verification_tier` — tier moved off `FounderProfile`/
  `InvestorProfile` onto the account. A founder's plan now covers every
  startup they own.
- `FounderProfile.user_id` is no longer unique — founders can own
  multiple startup profiles, capped by `PROFILE_LIMITS` in
  `routers/founder_profiles.py` (1/3/unlimited).
- `FounderProfile.published_until` — premium's 90-day auto-unpublish
  window; checked lazily on read (`_maybe_auto_unpublish`), not via a
  background job.
- New endpoints in `founder_profiles.py`: `GET /mine` (list all of my
  profiles), `POST /mine` (create, enforces the tier cap), `PUT`/`DELETE
  /{profile_id}` (manage a specific one). The old singular `/me`
  endpoints still work for anything not yet updated to the multi-profile
  UI — they operate on the founder's oldest profile.
- `connections.py`: founder-initiated requests (`POST /to-investor`) now
  require `founder_profile_id` (which startup is pitching) and are
  capped at 5 per rolling 30 days for `basic` founders, counted across
  all their profiles. `/sent` and `/received` aggregate across every
  profile a founder owns.
- `analytics.py`: founder analytics aggregate across all of a founder's
  profiles (not just one).
- `founder_subscriptions` + `investor_subscriptions` merged into one
  `subscriptions` table, keyed by `user_id` — both roles now use the
  exact same billing code path.

## Setup
1. Create **8** recurring products in the Bachs dashboard at the new
   prices above (basic/premium × monthly/annual × founder/investor).
   Paste all 8 `prod_...` ids into env vars — see `.env.example`.
2. Run `migration_multi_profile.sql` (after the earlier
   `migration_add_billing.sql` has already been applied). It:
   - adds `users.verification_tier`, carrying over whatever each
     founder/investor's profile already had
   - drops `verification_tier` from `founder_profiles`/
     `investor_profiles`
   - drops the unique constraint on `founder_profiles.user_id`
   - adds `founder_profiles.published_until`
   - creates `subscriptions`, copying rows over from the old
     `founder_subscriptions`/`investor_subscriptions` tables (left
     those old tables in place — drop them manually once you've
     confirmed the copy looks right; the commented-out `DROP TABLE`
     lines are at the bottom of the migration file)
3. Update the 8 product-id env vars + keep `BACHS_API_KEY` /
   `BACHS_WEBHOOK_SECRET` as before in Render.

## Notes
- `starter` founders are blocked outright from `POST /to-investor` (403)
  — that feature requires `basic` or `premium`. `basic` is additionally
  capped at 5/30 days; `premium` is unlimited.
- The frontend still needs: a profile switcher UI, "create new profile"
  flow respecting the tier cap, and updated pricing cards. Not yet
  built as of this backend pass.
