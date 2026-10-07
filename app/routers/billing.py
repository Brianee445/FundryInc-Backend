"""
Paid verification tiers via Bachs, for both founders and investors.

Tiers: starter (free, default) -> basic -> premium — an ACCOUNT-level
subscription on User.verification_tier, not tied to any one startup
profile. For founders this means the plan covers every startup they
manage (how many they're allowed to create is enforced in
routers/founder_profiles.py; how many intro messages they can send is
enforced in routers/connections.py).

Flow:
  1. POST /api/v1/billing/checkout {tier, interval} -> creates a Bachs
     checkout session against the matching recurring product and returns
     the hosted checkout URL. Frontend redirects the browser there.
  2. User pays on Bachs' hosted page. Bachs redirects back to
     frontend_billing_return_url — UX only, never grants the tier itself.
  3. Bachs POSTs collection.succeeded (and later invoice.paid on renewal,
     subscription.canceled on cancellation) to
     POST /api/v1/billing/webhook. That webhook is the only thing that
     ever changes verification_tier — per Bachs' own guidance, webhooks
     are the source of truth for fulfilment, never the client redirect.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import (
    BillingEvent,
    BillingIntervalEnum,
    Subscription,
    SubscriptionStatusEnum,
    User,
    VerificationTierEnum,
)
from app.schemas import CheckoutRequest, CheckoutResponse, SubscriptionStatusResponse
from app.services import bachs
from app.services.webhook_verify import verify_signature

router = APIRouter(prefix="/api/v1/billing", tags=["Billing"])

# (role, tier, interval) -> settings attribute name. starter is free and
# deliberately absent — there is no checkout for it.
_PRODUCT_ID_SETTINGS = {
    ("founder", "basic", "monthly"): "bachs_founder_basic_monthly_product_id",
    ("founder", "basic", "annual"): "bachs_founder_basic_annual_product_id",
    ("founder", "premium", "monthly"): "bachs_founder_premium_monthly_product_id",
    ("founder", "premium", "annual"): "bachs_founder_premium_annual_product_id",
    ("investor", "basic", "monthly"): "bachs_investor_basic_monthly_product_id",
    ("investor", "basic", "annual"): "bachs_investor_basic_annual_product_id",
    ("investor", "premium", "monthly"): "bachs_investor_premium_monthly_product_id",
    ("investor", "premium", "annual"): "bachs_investor_premium_annual_product_id",
}


def _product_id(role: str, tier: str, interval: str) -> str:
    attr = _PRODUCT_ID_SETTINGS.get((role, tier, interval))
    return getattr(settings, attr, "") if attr else ""


def _require_billable_role(current_user: User) -> str:
    if current_user.role not in ("founder", "investor"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Billing is only available to founders and investors.")
    return current_user.role


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout(
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Founder or investor. Starts a Bachs checkout for the chosen paid tier + billing interval."""
    role = _require_billable_role(current_user)

    product_id = _product_id(role, payload.tier, payload.interval)
    if not product_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Billing is not configured yet for this plan.")

    try:
        session = bachs.create_checkout_session(
            product_id=product_id,
            customer_email=current_user.email,
            metadata={
                "role": role,
                "user_id": str(current_user.id),
                "tier": payload.tier,
                "interval": payload.interval,
            },
        )
    except bachs.BachsError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    checkout_url = session.get("checkout_url") or session.get("url")
    if not checkout_url:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Bachs did not return a checkout URL.")

    # Record the pending subscription now, keyed by the checkout session id,
    # so the webhook has a row to attach the real bachs_subscription_id to
    # once payment completes. Reads of verification_tier never look at
    # this row directly, only at User.verification_tier, which the
    # webhook sets.
    existing = db.query(Subscription).filter(Subscription.user_id == current_user.id).first()
    if existing is None:
        existing = Subscription(
            user_id=current_user.id,
            tier=VerificationTierEnum(payload.tier),
            interval=BillingIntervalEnum(payload.interval),
        )
        db.add(existing)
    else:
        existing.tier = VerificationTierEnum(payload.tier)
        existing.interval = BillingIntervalEnum(payload.interval)
    existing.bachs_checkout_session_id = session.get("id")
    existing.currency = "NGN"
    db.commit()

    return CheckoutResponse(checkout_url=checkout_url)


@router.get("/subscription", response_model=SubscriptionStatusResponse)
def get_my_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_billable_role(current_user)
    sub = db.query(Subscription).filter(Subscription.user_id == current_user.id).first()

    return SubscriptionStatusResponse(
        verification_tier=current_user.verification_tier.value,
        tier=sub.tier.value if sub else None,
        interval=sub.interval.value if sub else None,
        status=sub.status.value if sub else None,
        current_period_end=sub.current_period_end if sub else None,
    )


@router.post("/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_my_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cancels at Bachs immediately. verification_tier is NOT downgraded here —
    that only happens once the subscription.canceled webhook confirms it,
    so the badge/feature access stays consistent with "webhooks are the
    source of truth for fulfilment" and the user keeps what they already
    paid for through the end of the current period.
    """
    _require_billable_role(current_user)
    sub = db.query(Subscription).filter(Subscription.user_id == current_user.id).first()
    if sub is None or not sub.bachs_subscription_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active subscription found.")

    try:
        bachs.cancel_subscription(sub.bachs_subscription_id)
    except bachs.BachsError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def bachs_webhook(
    request: Request,
    db: Session = Depends(get_db),
    bachs_signature: str | None = Header(default=None, alias="Bachs-Signature"),
    x_bachs_signature: str | None = Header(default=None, alias="X-Bachs-Signature"),
):
    """
    Receives Bachs webhook events. No auth dependency — Bachs calls this,
    not a logged-in Fundry user — so the HMAC signature check below IS the
    authentication for this endpoint. Confirm the actual header name Bachs
    sends against their webhook docs; both common conventions are checked.
    """
    raw_body = await request.body()
    signature = bachs_signature or x_bachs_signature

    if not verify_signature(raw_body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature.")

    event = json.loads(raw_body)
    event_id = event.get("id") or event.get("event_id")
    event_type = event.get("type") or event.get("event")

    if not event_id or not event_type:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed webhook payload.")

    # Idempotency: Bachs (like most webhook senders) may redeliver the same
    # event. If we've already logged this event id, ack it without
    # re-applying side effects.
    if db.query(BillingEvent).filter(BillingEvent.bachs_event_id == event_id).first() is not None:
        return {"status": "already_processed"}

    db.add(BillingEvent(bachs_event_id=event_id, event_type=event_type, payload=raw_body.decode("utf-8")))
    db.commit()

    data = event.get("data", event)
    metadata = data.get("metadata", {}) or {}
    user_id = metadata.get("user_id")

    if event_type in ("collection.succeeded", "checkout.completed", "invoice.paid"):
        if not user_id:
            return {"status": "ignored_no_metadata"}

        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            return {"status": "ignored_unknown_user"}

        sub = db.query(Subscription).filter(Subscription.user_id == user.id).first()
        if sub is None:
            sub = Subscription(
                user_id=user.id,
                tier=VerificationTierEnum(metadata.get("tier", "basic")),
                interval=BillingIntervalEnum(metadata.get("interval", "monthly")),
            )
            db.add(sub)
        else:
            sub.tier = VerificationTierEnum(metadata.get("tier", sub.tier.value))
            sub.interval = BillingIntervalEnum(metadata.get("interval", sub.interval.value))

        sub.status = SubscriptionStatusEnum.active
        sub.bachs_subscription_id = data.get("subscription_id") or data.get("subscription", {}).get("id")
        sub.bachs_customer_id = data.get("customer_id") or data.get("customer", {}).get("id")
        period_end = data.get("current_period_end") or data.get("subscription", {}).get("current_period_end")
        if period_end:
            sub.current_period_end = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        amount = data.get("amount")
        if amount is not None:
            sub.amount = amount
        sub.currency = data.get("currency", "NGN")

        user.verification_tier = sub.tier
        db.commit()

    elif event_type in ("subscription.canceled", "subscription.expired", "invoice.payment_failed"):
        bachs_sub_id = data.get("subscription_id") or data.get("id")

        sub = db.query(Subscription).filter(Subscription.bachs_subscription_id == bachs_sub_id).first()
        if sub is None:
            return {"status": "ignored_unknown_subscription"}

        if event_type == "invoice.payment_failed":
            sub.status = SubscriptionStatusEnum.past_due
        else:
            sub.status = SubscriptionStatusEnum.canceled
            sub.canceled_at = datetime.now(timezone.utc)
            user = db.query(User).filter(User.id == sub.user_id).first()
            if user is not None:
                user.verification_tier = VerificationTierEnum.starter
        db.commit()

    return {"status": "processed"}
