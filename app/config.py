from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Environment-driven configuration.

    pydantic-settings reads these from the process environment (and from a
    .env file locally, via python-dotenv), so nothing here should ever be
    hardcoded — especially SECRET_KEY, which must be a long random value set
    directly in the Render dashboard for production.
    """

    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Must match the OAuth client ID configured in Google Cloud Console.
    # Used as the required `audience` when verifying Google ID tokens, so a
    # token issued for a different app can never be replayed against this API.
    google_client_id: str = ""

    # Supabase Storage — used for founder profile picture / gallery / demo
    # video uploads (app/routers/media.py). The service role key is used
    # server-side only (never sent to the frontend) so uploads work
    # regardless of Storage RLS policies; this backend does its own auth
    # via require_role("founder"), so bypassing RLS here is intentional,
    # not a shortcut around access control.
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_storage_bucket: str = "founder-media"

    # Paystack (payments/billing). Test vs live is just the key prefix —
    # sk_test_... or sk_live_... — against the same api.paystack.co; there
    # is no separate sandbox base URL like Bachs had. See
    # app/services/paystack.py. Unlike Bachs, Paystack has no separate
    # webhook-signing secret: inbound webhooks are verified with this same
    # secret key (see app/services/webhook_verify.py).
    paystack_secret_key: str = ""
    paystack_public_key: str = ""
    # Paystack plan codes (PLN_...) for each paid (tier, interval)
    # combination, per role. starter is free and has no plan. Create all
    # 8 recurring plans in the Paystack dashboard first — basic
    # ₦2,000/mo & ₦24,000/yr; premium ₦5,000/mo & ₦60,000/yr (₦50,000/yr
    # as the discounted/promo price, ₦10,000 savings) — for both founders
    # and investors, then paste their PLN_... codes below. A plan's price
    # is fixed at creation in the Paystack dashboard; these codes are
    # how initialize_transaction() in paystack.py picks the right one.
    paystack_founder_basic_monthly_plan_code: str = ""
    paystack_founder_basic_annual_plan_code: str = ""
    paystack_founder_premium_monthly_plan_code: str = ""
    paystack_founder_premium_annual_plan_code: str = ""
    paystack_investor_basic_monthly_plan_code: str = ""
    paystack_investor_basic_annual_plan_code: str = ""
    paystack_investor_premium_monthly_plan_code: str = ""
    paystack_investor_premium_annual_plan_code: str = ""
    # Where Paystack sends the browser back to after payment (whether it
    # succeeded or the user backed out — Paystack doesn't distinguish
    # with separate URLs the way Bachs did). Fulfilment itself never
    # depends on this — only the webhook does — this is purely for UX.
    frontend_billing_return_url: str = "https://fundry-inc.vercel.app/billing?billing=success"
    frontend_billing_cancel_url: str = "https://fundry-inc.vercel.app/billing?billing=cancelled"

    class Config:
        env_file = ".env"
        # Field names above are lower_snake_case; environment variables are
        # conventionally UPPER_SNAKE_CASE. This makes pydantic-settings match
        # DATABASE_URL -> database_url, SECRET_KEY -> secret_key, etc.
        case_sensitive = False


settings = Settings()
