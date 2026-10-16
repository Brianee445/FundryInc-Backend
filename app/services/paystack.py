"""
Thin client for the Paystack payments/billing API.

Paystack conventions this client follows (per their docs):
  - Money is always in the smallest currency unit (kobo for NGN) — e.g.
    ₦2,000 is sent as amount=200000. Never a decimal string. See
    to_kobo() below; every amount this module sends has already been
    through it.
  - There is no separate "checkout session" API. A subscription starts
    by initializing a transaction with a `plan` code attached — Paystack
    creates the subscription once that first payment succeeds. See
    initialize_transaction().
  - Test vs live is a key prefix, not a different base URL: sk_test_...
    or sk_live_... against the same https://api.paystack.co. Swapping
    settings.paystack_secret_key is the entire environment switch.
  - Webhooks (charge.success, subscription.create, subscription.disable)
    are the source of truth for fulfilment. This client never trusts the
    frontend redirect alone — see app/routers/billing.py's webhook
    handler.
  - Cancelling a subscription requires BOTH its subscription_code and
    its email_token — Paystack hands both back on subscription.create
    and on a subscription fetch, never on our own say-so. See
    disable_subscription().
"""

import requests

from app.config import settings

PAYSTACK_BASE_URL = "https://api.paystack.co"


class PaystackError(Exception):
    """Raised when the Paystack API returns an error response."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def to_kobo(naira_amount: float) -> int:
    """Paystack amounts are always the smallest unit — kobo for NGN."""
    return round(naira_amount * 100)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.paystack_secret_key}",
        "Content-Type": "application/json",
    }


def initialize_transaction(
    plan_code: str,
    amount_naira: float,
    customer_email: str,
    metadata: dict | None = None,
) -> dict:
    """
    Starts the first payment on a plan. Paystack creates the subscription
    once this transaction succeeds — there's no separate "create
    subscription" call needed for the common case of a customer signing
    up for the first time (see Paystack's own subscriptions guide: adding
    a plan code to a transaction is one of their two documented ways to
    create a subscription, and the simpler one since it needs no
    customer_code up front).

    `amount_naira` is sent for clarity even though Paystack ignores the
    amount field once a plan is attached (the plan's own price wins) —
    passing it keeps this call honest about what the customer is being
    charged, and some Paystack test-mode flows do still look at it.

    `metadata` rides along on the transaction and comes back attached to
    the webhook event, which is how the webhook handler maps a payment
    back to a specific Fundry user.
    """
    payload = {
        "email": customer_email,
        "amount": to_kobo(amount_naira),
        "plan": plan_code,
        "callback_url": settings.frontend_billing_return_url,
    }
    if metadata:
        payload["metadata"] = metadata

    resp = requests.post(
        f"{PAYSTACK_BASE_URL}/transaction/initialize",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    if resp.status_code >= 400:
        raise PaystackError(f"Paystack transaction initialize failed: {resp.text}", resp.status_code)

    body = resp.json()
    if not body.get("status"):
        raise PaystackError(f"Paystack transaction initialize failed: {body.get('message')}")
    return body["data"]


def verify_transaction(reference: str) -> dict:
    """Confirms a transaction's status server-side by its reference — used to double-check a webhook's claim if ever needed."""
    resp = requests.get(
        f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code >= 400:
        raise PaystackError(f"Paystack transaction verify failed: {resp.text}", resp.status_code)
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(f"Paystack transaction verify failed: {body.get('message')}")
    return body["data"]


def disable_subscription(subscription_code: str, email_token: str) -> dict:
    """
    Cancels a subscription. Both the subscription_code (SUB_...) and
    email_token are required by Paystack's API and are only ever
    available from webhook data (subscription.create) — never something
    this app generates itself. See Subscription.paystack_subscription_code
    and Subscription.paystack_email_token in models.py for where these get
    stored.
    """
    resp = requests.post(
        f"{PAYSTACK_BASE_URL}/subscription/disable",
        headers=_headers(),
        json={"code": subscription_code, "token": email_token},
        timeout=15,
    )
    if resp.status_code >= 400:
        raise PaystackError(f"Paystack subscription disable failed: {resp.text}", resp.status_code)
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(f"Paystack subscription disable failed: {body.get('message')}")
    return body
