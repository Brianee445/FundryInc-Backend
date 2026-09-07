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

    class Config:
        env_file = ".env"
        # Field names above are lower_snake_case; environment variables are
        # conventionally UPPER_SNAKE_CASE. This makes pydantic-settings match
        # DATABASE_URL -> database_url, SECRET_KEY -> secret_key, etc.
        case_sensitive = False


settings = Settings()
