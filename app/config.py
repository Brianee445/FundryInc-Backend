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

    # Bachs (payments/billing). Sandbox first: sk_sandbox_ key against
    # https://sandbox-api.bachs.io. Swap to sk_live_ + https://api.bachs.io
    # to go live — see app/services/bachs.py.
    bachs_api_key: str = ""
    bachs_api_base_url: str = "https://sandbox-api.bachs.io"
    bachs_webhook_secret: str = ""
    # The Bachs product_id for the recurring "Founder Premium" (gold
    # verification) plan. Monthly and annual are two separate Bachs
    # products (different prices/intervals), not one product with a
    # parameter — create both in the Bachs dashboard and paste the ids here.
    # Bachs product_ids for each paid (tier, interval) combination, per
    # role. starter is free and has no product id. Create all 8 recurring
    # products in the Bachs dashboard first (₦3,000/mo & ₦30,000/yr basic,
    # ₦5,000/mo & ₦50,000/yr premium — annual = 10x monthly per plan — for
    # both founders and investors), then paste their prod_... ids below.
    bachs_founder_basic_monthly_product_id: str = ""
    bachs_founder_basic_annual_product_id: str = ""
    bachs_founder_premium_monthly_product_id: str = ""
    bachs_founder_premium_annual_product_id: str = ""
    bachs_investor_basic_monthly_product_id: str = ""
    bachs_investor_basic_annual_product_id: str = ""
    bachs_investor_premium_monthly_product_id: str = ""
    bachs_investor_premium_annual_product_id: str = ""
    # Where Bachs' hosted checkout redirects the browser after payment.
    # Fulfilment itself never depends on this — only the webhook does —
    # this is purely for UX (what the user sees after paying).
    frontend_billing_return_url: str = "http://localhost:3000/billing?billing=success"
    frontend_billing_cancel_url: str = "http://localhost:3000/billing?billing=cancelled"

    class Config:
        env_file = ".env"
        # Field names above are lower_snake_case; environment variables are
        # conventionally UPPER_SNAKE_CASE. This makes pydantic-settings match
        # DATABASE_URL -> database_url, SECRET_KEY -> secret_key, etc.
        case_sensitive = False


settings = Settings()
