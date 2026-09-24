"""
Thin client for the Bachs payments/billing API.

Bachs conventions this client follows (per their docs):
  - Money is always a decimal string at the currency's precision
    (e.g. "5000.00"), paired with an ISO 4217 currency code — never minor
    units.
  - Sandbox first: sk_sandbox_ keys against sandbox-api.bachs.io; going
    live is just swapping the key + base URL to sk_live_ / api.bachs.io.
    Both are driven by settings.bachs_api_key / bachs_api_base_url, so
    nothing here hardcodes an environment.
  - Subscriptions are never created directly — you create a checkout
    session against a recurring product, and the subscription comes into
    existence once that checkout completes. See create_checkout_session().
  - Webhooks (collection.succeeded, etc.) are the source of truth for
    fulfilment. This client never trusts the checkout redirect alone —
    see app/routers/billing.py's webhook handler.
"""

import requests

from app.config import settings


class BachsError(Exception):
    """Raised when the Bachs API returns an error response."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.bachs_api_key}",
        "Content-Type": "application/json",
    }


def create_checkout_session(
    product_id: str,
    customer_email: str,
    metadata: dict | None = None,
) -> dict:
    """
    Start a checkout for a single product (quantity 1). For a recurring
    product this is also how a subscription gets created — Bachs has no
    separate create-subscription endpoint.

    `metadata` rides along on the checkout session and comes back attached
    to the webhook event, which is how the webhook handler below maps a
    payment back to a specific Fundry founder profile.
    """
    payload = {
        "product_cart": [{"product_id": product_id, "quantity": 1}],
        "customer": {"email": customer_email},
        "return_url": settings.frontend_billing_return_url,
        "cancel_url": settings.frontend_billing_cancel_url,
    }
    if metadata:
        payload["metadata"] = metadata

    resp = requests.post(
        f"{settings.bachs_api_base_url}/v1/checkout-sessions",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    if resp.status_code >= 400:
        raise BachsError(f"Bachs checkout-session creation failed: {resp.text}", resp.status_code)
    return resp.json()


def get_subscription(subscription_id: str) -> dict:
    """Fetch a subscription by id (sub_...) — used to confirm status/period on renewal webhooks."""
    resp = requests.get(
        f"{settings.bachs_api_base_url}/v1/subscriptions/{subscription_id}",
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code >= 400:
        raise BachsError(f"Bachs subscription lookup failed: {resp.text}", resp.status_code)
    return resp.json()


def cancel_subscription(subscription_id: str) -> dict:
    """Cancel a subscription (e.g. when a founder downgrades from the dashboard)."""
    resp = requests.post(
        f"{settings.bachs_api_base_url}/v1/subscriptions/{subscription_id}/cancel",
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code >= 400:
        raise BachsError(f"Bachs subscription cancellation failed: {resp.text}", resp.status_code)
    return resp.json()
