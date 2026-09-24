"""
HMAC verification for inbound Bachs webhooks.

Bachs signs webhook payloads with an HMAC-SHA256 secret (set in the Bachs
dashboard, mirrored here as settings.bachs_webhook_secret). We verify the
signature before trusting any payload — an attacker who knows a founder's
profile id could otherwise POST a fake collection.succeeded and grant
themselves premium for free.

The exact header name (commonly `Bachs-Signature` or `X-Bachs-Signature`)
should be confirmed against Bachs' webhook docs when wiring this up for
real; app/routers/billing.py currently checks both, in that order, and
this stays a plain constant-time comparison either way.
"""

import hashlib
import hmac

from app.config import settings


def verify_signature(payload_body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not settings.bachs_webhook_secret:
        return False

    expected = hmac.new(
        settings.bachs_webhook_secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
