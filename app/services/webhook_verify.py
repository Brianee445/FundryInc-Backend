"""
HMAC verification for inbound Paystack webhooks.

Paystack signs every webhook with an HMAC-SHA512 digest of the RAW
request body, hex-encoded, keyed with your Paystack secret key (the
same sk_test_.../sk_live_... used for API calls — there's no separate
webhook-signing secret like some other providers use). The signature
arrives in the x-paystack-signature header. We verify it before
trusting any payload — an attacker who knows a user's id could otherwise
POST a fake charge.success and grant themselves premium for free.

Verify against the RAW bytes, not a re-serialized/parsed version of the
body — re-serializing JSON can reorder keys or change whitespace and
break the signature even though the content is "the same".
"""

import hashlib
import hmac

from app.config import settings


def verify_signature(payload_body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not settings.paystack_secret_key:
        return False

    expected = hmac.new(
        settings.paystack_secret_key.encode("utf-8"),
        payload_body,
        hashlib.sha512,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
