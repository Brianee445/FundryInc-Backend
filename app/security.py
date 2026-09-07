from datetime import datetime, timedelta, timezone

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

# bcrypt via passlib — the standard choice for password hashing; it's slow by
# design (that's the point) and handles salting internally.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _pwd_context.verify(plain_password, hashed_password)


def create_access_token(subject: str, role: str) -> str:
    """
    Build a signed JWT for a logged-in user.

    `subject` is the user's id (kept as a string per JWT convention — the
    "sub" claim). `role` is embedded directly in the token so the API can
    authorize a request without a database round trip on every call.
    """
    expire_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {"sub": subject, "role": role, "exp": expire_at}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> dict:
    """Raises jose.JWTError if the token is invalid, expired, or tampered with."""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as exc:
        raise exc


class GoogleTokenError(Exception):
    """Raised when a Google ID token fails verification."""


def verify_google_id_token(id_token_str: str) -> dict:
    """
    Verify a Google ID token (the `credential` returned by Google Identity
    Services on the frontend) and return its claims.

    This checks the token's signature against Google's published public
    keys, its expiry, and that it was issued for *this* app's client ID
    (the `audience` check) — without that last check, a token minted for a
    completely different Google-integrated app could be replayed here.
    """
    try:
        claims = google_id_token.verify_oauth2_token(
            id_token_str,
            google_requests.Request(),
            audience=settings.google_client_id,
        )
    except ValueError as exc:
        raise GoogleTokenError(str(exc)) from exc

    if not claims.get("email_verified", False):
        raise GoogleTokenError("Google account email is not verified.")

    return claims
